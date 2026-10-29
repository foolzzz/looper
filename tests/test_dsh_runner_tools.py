"""Installed DSH tools, confined files, in-trace steering and read-only forks."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from dsh_installed_stand_in import MessagesServer, Reply, tool
from dsh_runner_support import dsh_supervisor, guard_env, raw_runner, require_dsh, session_params
from looper.agents.runtimes.runner import dsh_command
from looper.agents.runtimes.runner.supervisor import RunnerUnavailable
from looper.agents.runtimes.runner.turn import RunnerTurn
from test_dsh_runner import harness
from test_dsh_runner_sessions import stored
from test_dsh_runner_turns import Client as TurnClient, params_for, run

pytestmark = pytest.mark.installed


class Client(TurnClient):
    def __init__(self, send, receive, *, answer=None):
        super().__init__(send, receive)
        self.answer = answer
        self.calls = []

    async def read(self):
        frame = await super().read()
        if frame.get("method") == "tools/call":
            self.calls.append(frame)
            if self.answer is not None:
                await self.send({"jsonrpc": "2.0", "id": frame["id"], "result": self.answer})
        return frame

    async def prompt(self, sid, trace="trace", text="Disposable prompt", tools=()):
        return await self.call("session/prompt", {
            "session_id": sid, "trace_id": trace, "text": text, "tools": list(tools)})

    async def tool_call(self):
        while not self.calls:
            await self.read()
        return self.calls[-1]

    async def steer(self, sid, *, trace="trace", ident="steer-1", text="Disposable steering", code=None):
        return await self.call("session/steer", {"session_id": sid, "trace_id": trace,
            "message_id": ident, "text": text, "if_idle": "reject"}, code=code)


def writable(params):
    return {**params, "tools": {"builtin": ["read", "write", "edit", "glob", "grep", "bash"],
                               "platform": [], "read_only": False}}


def platform(name):
    return {"name": name, "description": "Disposable platform tool",
            "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}}}}


def bash(command, **args):
    return tool("bash", {"description": "Disposable shell probe", "command": command, **args})


async def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition did not become true before the deadline"
        await asyncio.sleep(.03)


def alive(pid):
    # Zombies have exited but may still be awaiting the system reaper.
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True,
                            text=True, timeout=3)
    return result.returncode == 0 and bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


def request_text(request):
    return json.dumps(request["body"]["messages"])


def test_bash_runs_with_the_session_environment_only(tmp_path, monkeypatch):
    harness(tmp_path, monkeypatch, """rpc.method('test/parent-env', () => ({
  sentinel: process.env.LOOPER_PARENT_SENTINEL
}));""")

    async def exercise():
        _, installed = require_dsh()
        replies = [[bash("env; pwd")], [bash("sleep 5", timeoutMs=60000)],
                   [bash("exit 3")], [bash("pwd", workdir=str(tmp_path))],
                   [tool("write", {"file_path": "new/nested.txt", "content": "WRITABLE-PATH-CONTROL"})], ["Done"]]
        with MessagesServer(replies) as server:
            env = {**guard_env(tmp_path, server.port), "LOOPER_PARENT_SENTINEL": "parent-only-value"}
            async with raw_runner(tmp_path, installed, env=env) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                assert await client.call("test/parent-env") == {"sentinel": "parent-only-value"}
                params = writable(params_for(tmp_path, server))
                params["env"] = {"LOOPER_SESSION_VALUE": "session-only-value", "PATH": "/usr/bin:/bin"}
                params["limits"]["tool_timeout_s"] = 1
                sid = (await client.call("session/new", params))["session_id"]
                await client.prompt(sid)
                assert (await client.settled(sid))["outcome"] == "completed"
                results = client.updates(sid, kind="tool_result")
                assert len(results) == 5
                assert "LOOPER_SESSION_VALUE=session-only-value" in results[0]["text"]
                assert f"HOME={params['home']}" in results[0]["text"] and params["cwd"] in results[0]["text"]
                assert "LOOPER_PARENT_SENTINEL" not in results[0]["text"]
                assert "parent-only-value" not in results[0]["text"]
                assert "timed out after 1 s" in results[1]["text"]
                assert (server.requests[2]["at"] - server.requests[1]["at"]) / 1e9 < 3
                assert "exit code 3" in results[2]["text"] and results[2]["is_error"] is False
                assert results[3]["is_error"] and "LOOPER_PATH" in results[3]["text"]
                assert not results[4]["is_error"]
                assert (Path(params["cwd"]) / "new/nested.txt").read_text() == "WRITABLE-PATH-CONTROL"
                assert (await client.call("runner/health"))["tool_processes"] == 0
    run(exercise())


@pytest.mark.parametrize("end", ["cancel", "runner_loss"])
def test_cancel_and_runner_loss_end_tool_processes(tmp_path, end):
    async def exercise():
        require_dsh()
        pids = []
        with MessagesServer([[bash("sleep 300 & child=$!; echo $$ $child > pids; wait")], ["Done"]]) as server:
            async with dsh_supervisor(tmp_path, env=guard_env(tmp_path, server.port)) as supervisor:
                lease = await supervisor.acquire("dsh")
                params = writable(session_params(lease))
                params["provider"].update(base_url=server.url, reasoning=False)
                params["limits"]["tool_output_bytes"] = 102400
                params["env"] = {"PATH": "/usr/bin:/bin"}
                sid = await lease.open_session(params)

                async def limits():
                    return {"run_cap": None, "task_cap": None, "task_spent": None}

                turn = RunnerTurn(session_id=sid, trace_id="trace", role="manager",
                    tools=SimpleNamespace(), provider_id="account", model=params["provider"]["model"],
                    price=lambda model, usage: 0, limits=limits)
                try:
                    await lease.bridge.begin_turn(sid, "trace", lambda event: None, turn, text="Start shell")
                    file = lease.directory / "pids"
                    await wait_until(lambda: file.exists() and len(file.read_text().split()) == 2)
                    pids = list(map(int, file.read_text().split()))
                    assert all(alive(pid) for pid in pids), "positive control: shell and child must both be alive"
                    assert (await lease.bridge.health())["tool_processes"] == 1
                    if end == "cancel":
                        await lease.bridge.cancel_turn(sid, "trace")
                        assert (await lease.bridge.wait_settled(sid, "trace")).outcome == "cancelled"
                        await wait_until(lambda: not any(alive(pid) for pid in pids), 5)
                    else:
                        lease._generation.process.kill()
                        await wait_until(lambda: not any(alive(pid) for pid in pids), 10)
                        replacement = None
                        deadline = time.monotonic() + 10
                        while replacement is None and time.monotonic() < deadline:
                            try:
                                replacement = await supervisor.acquire("dsh")
                            except RunnerUnavailable:
                                await asyncio.sleep(.1)
                        assert replacement is not None, "supervisor must recover after runner loss"
                        assert (await replacement.bridge.health())["tool_processes"] == 0
                        await replacement.release()
                finally:
                    for pid in pids:
                        if alive(pid):
                            os.kill(pid, signal.SIGKILL)
                    await lease.release()
    run(exercise())


def test_read_only_session_cannot_write_or_escape(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                params = params_for(tmp_path, server)
                cwd = Path(params["cwd"])
                outside = cwd.parent / "outside.txt"
                outside.write_text("OUTSIDE-CONTENT-MUST-NOT-LEAK")
                (cwd / "link.txt").symlink_to(outside)
                physical = tmp_path / "external"
                (physical / "child").mkdir(parents=True)
                (physical / "secret.txt").write_text("OUTSIDE-CONTENT-MUST-NOT-LEAK")
                (cwd / "dir-link").symlink_to(physical / "child", target_is_directory=True)
                (cwd / "dangling.txt").symlink_to(cwd / "missing.txt")
                (cwd / "inside.txt").write_text("INSIDE-CONTENT-POSITIVE-CONTROL")
                calls = [tool("write", {"file_path": "created.txt", "content": "bad"}),
                    tool("edit", {"file_path": "inside.txt", "old_string": "INSIDE", "new_string": "changed"}),
                    bash("touch created.txt"), tool("read", {"file_path": "../outside.txt"}),
                    tool("read", {"file_path": str(outside)}), tool("read", {"file_path": "link.txt"}),
                    tool("glob", {"pattern": "../*"}), tool("grep", {"pattern": ".", "include": "../*"}),
                    tool("read", {"file_path": "dangling.txt"}),
                    tool("read", {"file_path": "dir-link/../secret.txt"}), tool("read", {"file_path": "~/outside.txt"}),
                    tool("read", {"file_path": "inside.txt"})]
                server.replies.extend([[call] for call in calls] + [["Done"]])
                sid = (await client.call("session/new", params))["session_id"]
                await client.prompt(sid)
                assert (await client.settled(sid))["outcome"] == "completed"
                results = client.updates(sid, kind="tool_result")
                assert len(results) == len(calls)
                for result in results[:-1]:
                    assert result["is_error"] and any(code in result["text"] for code in ("LOOPER_READ_ONLY", "LOOPER_PATH")), result
                assert "INSIDE-CONTENT-POSITIVE-CONTROL" in results[-1]["text"]
                assert not results[-1]["is_error"] and not (cwd / "created.txt").exists()
                assert (cwd / "inside.txt").read_text() == "INSIDE-CONTENT-POSITIVE-CONTROL"
                for request in server.requests:
                    assert {item["name"] for item in request["body"]["tools"]} == {"read", "glob", "grep"}
                    assert "OUTSIDE-CONTENT-MUST-NOT-LEAK" not in request_text(request)
    run(exercise())


@pytest.mark.parametrize("is_error", [False, True])
def test_platform_tool_round_trip_and_snapshot(tmp_path, is_error):
    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([[tool("looper_a", {"value": "probe"})], ["First done"],
                             [tool("looper_a", {})], [tool("looper_b", {})], ["Second done"], ["Valid after invalid"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                answer = "VERBATIM PLATFORM RESULT " + "x" * 60000
                client = Client(send, receive, answer={"text": answer, "is_error": is_error})
                await client.start()
                sid = (await client.call("session/new", params_for(tmp_path, server)))["session_id"]
                await client.prompt(sid, tools=[platform("looper_a")])
                assert (await client.settled(sid))["outcome"] == "completed"
                call, = client.calls
                message, = client.updates(sid, kind="message")[0]["content"]
                assert call["params"] == {"session_id": sid, "trace_id": "trace", "call_id": message["id"],
                    "name": "looper_a", "arguments": {"value": "probe"}}
                result, = client.updates(sid, kind="tool_result")
                assert result["text"] == ("Error: " if is_error else "") + answer
                assert result["is_error"] is is_error
                wire_results = [block for msg in server.requests[1]["body"]["messages"]
                    for block in msg["content"] if isinstance(block, dict) and block.get("type") == "tool_result"]
                assert any(answer in json.dumps(block) and block.get("is_error", False) is is_error
                           for block in wire_results)
                await client.prompt(sid, "second", tools=[platform("looper_b")])
                assert (await client.settled(sid, "second"))["outcome"] == "completed"
                names = {item["name"] for item in server.requests[2]["body"]["tools"]}
                assert "looper_b" in names and "looper_a" not in names
                assert len(client.calls) == 2 and client.calls[-1]["params"]["name"] == "looper_b"
                denied = client.updates(sid, "second", "tool_result")[0]
                assert denied["is_error"]
                invalid = [[platform("same"), platform("same")], [platform("read")], [platform("run_code")],
                           [{**platform("bad"), "inputSchema": []}],
                           [{**platform("bad"), "inputSchema": {"type": "string"}}],
                           [{**platform("bad.name")}], [{**platform("bad"), "description": None}]]
                for snapshot in invalid:
                    await client.call("session/prompt", {"session_id": sid, "trace_id": "invalid",
                        "text": "Rejected snapshot", "tools": snapshot}, code=-32602)
                assert not client.updates(sid, "invalid")
                await client.prompt(sid, "valid", tools=[platform("looper_b")])
                assert (await client.settled(sid, "valid"))["outcome"] == "completed"
    run(exercise())


def test_cancel_during_a_platform_tool(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([[tool("looper_held", {})]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                sid = (await client.call("session/new", params_for(tmp_path, server)))["session_id"]
                await client.prompt(sid, tools=[platform("looper_held")])
                call = await client.tool_call()
                await client.call("session/cancel", {"session_id": sid, "trace_id": "trace"})
                assert (await client.settled(sid))["outcome"] == "cancelled"
                await send({"jsonrpc": "2.0", "id": call["id"], "result": {"text": "Late answer", "is_error": False}})
                assert (await client.call("runner/health"))["running"] == 0
                assert len(client.updates(sid, kind="settled")) == 1 and len(server.requests) == 1
                assert not any(frame.get("method") == "rpc/cancel" for frame in client.frames)
    run(exercise())


def test_steer_applies_in_the_same_trace(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([[bash("echo ready > started; while [ ! -f gate ]; do sleep .05; done")], ["Done"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                params = writable(params_for(tmp_path, server))
                sid = (await client.call("session/new", params))["session_id"]
                await client.prompt(sid)
                await client.requested(server)
                await wait_until(lambda: (Path(params["cwd"]) / "started").exists())
                await client.steer(sid, trace="wrong", code=-32005)
                for bad in ({"if_idle": "start"}, {"message_id": ""}, {"text": ""}):
                    await client.call("session/steer", {"session_id": sid, "trace_id": "trace", "if_idle": "reject",
                        "message_id": "valid", "text": "valid", **bad}, code=-32602)
                queued = await client.steer(sid)
                assert queued == {"session_id": sid, "trace_id": "trace", "status": "queued"}
                assert await client.steer(sid) == queued
                (Path(params["cwd"]) / "gate").touch()
                assert (await client.settled(sid))["outcome"] == "completed"
                applied, = client.updates(sid, kind="steer_applied")
                assert applied["message_id"] == "steer-1" and applied["after_tool_batch"] is True
                assert applied["after_request_id"] == server.requests[0]["request_id"] and applied["new_turn"] is False
                assert not client.updates(sid, kind="steer_dropped")
                assert len(server.requests) == 2
                assert request_text(server.requests[1]).count("Disposable steering") == 1
                await client.steer(sid, code=-32004)
    run(exercise())


@pytest.mark.parametrize("ending", ["cancel", "final"])
def test_steer_is_dropped_never_lost(tmp_path, ending):
    async def exercise():
        _, installed = require_dsh()
        gate = threading.Event()
        replies = ([[bash("echo ready > started; sleep 300")]] if ending == "cancel"
                   else [Reply(["Final answer"], hold=gate)])
        with MessagesServer(replies) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                params = writable(params_for(tmp_path, server))
                sid = (await client.call("session/new", params))["session_id"]
                await client.prompt(sid)
                await client.requested(server)
                if ending == "cancel":
                    await wait_until(lambda: (Path(params["cwd"]) / "started").exists())
                await client.steer(sid, trace="wrong", code=-32005)
                await client.steer(sid)
                if ending == "cancel":
                    await client.call("session/cancel", {"session_id": sid, "trace_id": "trace"})
                else:
                    gate.set()
                await client.settled(sid)
                applied, dropped = client.updates(sid, kind="steer_applied"), client.updates(sid, kind="steer_dropped")
                assert len(applied) + len(dropped) == 1
                if ending == "cancel":
                    assert not applied and dropped[0]["reason"] == "cancelled"
                elif dropped:
                    assert dropped[0]["reason"] == "turn_ended"
                count = len(server.requests)
                await client.steer(sid, code=-32004)
                await client.call("runner/health")
                assert len(server.requests) == count
                settled_index = next(i for i, frame in enumerate(client.frames) if frame.get("method") == "session/update"
                    and frame["params"]["kind"] == "settled")
                assert not any(frame.get("method") == "budget/authorize" for frame in client.frames[settled_index + 1:])
    run(exercise())


def test_fork_continues_a_copy_without_writing_it(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([["Original done"], ["Consult done"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                params = params_for(tmp_path, server)
                params["role"] = "worker"
                sid = (await client.call("session/new", params))["session_id"]
                await client.prompt(sid, text="ORIGINAL-USER-TEXT")
                assert (await client.settled(sid))["outcome"] == "completed"
                await client.call("session/close", {"session_id": sid})
                state = dsh_command.state_root(Path(params["cwd"]).parents[1])
                directory = stored(state, sid)

                def hashes():
                    return {str(file.relative_to(directory)): hashlib.sha256(file.read_bytes()).hexdigest()
                            for file in directory.rglob("*") if file.is_file()}

                before = hashes()
                assert before, "positive control: source session must have persisted files"
                fork = {**params, "session_key": "fork", "fork_from": {"session_id": sid}}
                consult = (await client.call("session/new", fork))["session_id"]
                assert consult != sid
                fork_directory = stored(state, consult)
                await client.prompt(consult, text="Consult the finished run")
                assert (await client.settled(consult))["outcome"] == "completed"
                assert "ORIGINAL-USER-TEXT" in request_text(server.requests[1])
                await client.call("session/close", {"session_id": consult})
                assert not fork_directory.exists() and hashes() == before
                await client.call("session/new", {**fork, "fork_from": {"session_id": "unknown"}}, code=-32001)
                await client.call("session/new", {**fork, "tools": {**params["tools"], "read_only": False}}, code=-32008)
                empty = (await client.call("session/new", {**params, "session_key": "empty"}))["session_id"]
                await client.call("session/close", {"session_id": empty})
                await client.call("session/new", {**fork, "fork_from": {"session_id": empty}}, code=-32001)
    run(exercise())


def test_bash_schema_timeout_defaults_and_output_tail(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        bounded = [bash("sleep 5", **args) for args in ({}, {"timeoutMs": 0}, {"timeoutMs": -1})]
        output = bash("head -c 110000 /dev/zero | tr '\\000' x; printf END")
        with MessagesServer([[call] for call in [*bounded, output]] + [["Done"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                client = Client(send, receive)
                await client.start()
                params = writable(params_for(tmp_path, server))
                params["env"] = {"PATH": "/usr/bin:/bin"}
                params["limits"]["tool_timeout_s"] = 1
                sid = (await client.call("session/new", params))["session_id"]
                await client.prompt(sid)
                assert (await client.settled(sid))["outcome"] == "completed"
                shell, = [entry for entry in server.requests[0]["body"]["tools"] if entry["name"] == "bash"]
                assert shell["description"] == "Run a bash command in the task directory and return its combined output and exit code."
                assert shell["input_schema"] == {"type": "object", "properties": {
                    "description": {"type": "string"}, "command": {"type": "string"},
                    "timeoutMs": {"type": "number"}, "workdir": {"type": "string"}},
                    "required": ["description", "command"]}
                results = client.updates(sid, kind="tool_result")
                assert len(results) == 4
                for index, result in enumerate(results[:3]):
                    assert "timed out after 1 s" in result["text"] and not result["is_error"]
                    assert (server.requests[index + 1]["at"] - server.requests[index]["at"]) / 1e9 < 3
                assert results[3]["text"] == "[earlier output truncated]\n" + "x" * 102397 + "END\nexit code 0"
                assert not results[3]["is_error"]
    run(exercise())


@pytest.mark.parametrize("ending", ["sent", "close", "denied"])
def test_steer_queued_before_turn_start_reaches_one_terminal_state(tmp_path, monkeypatch, ending):
    session_file = dsh_command.entry().resolve().parent / "session.ts"
    harness(tmp_path, monkeypatch, f"""const {{sessions}} = await import({json.dumps(session_file.as_uri())});
const parked = new Map();
rpc.method('test/park-prompt', p => {{
  const record = sessions.get(p.session_id), agent = record.agent;
  const followup = agent.followup.bind(agent);
  agent.followup = message => {{
    parked.set(record.id, {{message, followup}});
    agent.send(message, 'next-turn', false);
  }};
  return {{}};
}});
rpc.method('test/park-state', p => {{
  const record = sessions.get(p.session_id);
  return {{started: record.turn.started, status: record.agent.status}};
}});
rpc.method('test/wake-prompt', p => {{
  const record = sessions.get(p.session_id), input = parked.get(record.id);
  record.agent.inbox.remove(input.message.id);
  input.followup(input.message);
  return {{}};
}});""")

    async def exercise():
        _, installed = require_dsh()
        with MessagesServer([[tool("read", {"file_path": "note.txt"})], ["Done"]]) as server:
            async with raw_runner(tmp_path, installed, env=guard_env(tmp_path, server.port)) as (_, send, receive):
                class BudgetClient(Client):
                    async def read(self):
                        self.denial = "run_cap" if ending == "denied" and self.authorized else None
                        return await super().read()

                client = BudgetClient(send, receive)
                await client.start()
                params = params_for(tmp_path, server)
                (Path(params["cwd"]) / "note.txt").write_text("Disposable note")
                sid = (await client.call("session/new", params))["session_id"]
                await client.call("test/park-prompt", {"session_id": sid})
                await client.prompt(sid)
                assert await client.call("test/park-state", {"session_id": sid}) == {"started": False, "status": "idle"}
                await client.steer(sid, text="QUEUED-BEFORE-TURN-START")
                assert await client.call("test/park-state", {"session_id": sid}) == {"started": False, "status": "idle"}
                assert not client.authorized and not server.requests
                if ending == "close":
                    await client.call("session/close", {"session_id": sid, "trace_id": "trace"})
                else:
                    await client.call("test/wake-prompt", {"session_id": sid})
                settled = await client.settled(sid)
                applied = client.updates(sid, kind="steer_applied")
                dropped = client.updates(sid, kind="steer_dropped")
                assert len(applied) + len(dropped) == 1
                if ending == "sent":
                    assert settled["outcome"] == "completed" and len(server.requests) == 2
                    assert "QUEUED-BEFORE-TURN-START" in request_text(server.requests[1])
                    assert applied[0]["message_id"] == "steer-1"
                    assert applied[0]["after_request_id"] == server.requests[0]["request_id"]
                    assert applied[0]["after_tool_batch"] is True
                else:
                    assert not applied and len(server.requests) == (0 if ending == "close" else 1)
                    assert len(client.authorized) == (0 if ending == "close" else 2)
                    assert dropped[0]["reason"] == ("session_closed" if ending == "close" else "turn_ended")
                    assert settled["outcome"] == ("cancelled" if ending == "close" else "capped")
                await client.call("runner/health")
                assert len(client.updates(sid, kind="settled")) == 1
    run(exercise())
