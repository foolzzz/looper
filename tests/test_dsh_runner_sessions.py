"""Installed DSH session ownership, persistence and isolation without model traffic."""
import asyncio
import json
import signal
import subprocess

import pytest

from dsh_runner_support import OFFER, dsh_supervisor, raw_lease, raw_runner, require_dsh, session_params
from looper.agents.runtimes.runner import dsh_command
from test_dsh_runner import harness

pytestmark = pytest.mark.installed
KEY = "synthetic-dsh-runner-key"


def run(coroutine):
    asyncio.run(asyncio.wait_for(coroutine, 120))


class Client:
    def __init__(self, send, receive):
        self.send, self.receive, self.counter = send, receive, 0

    async def call(self, method, params=None, *, code=None):
        self.counter += 1
        await self.send(method, params, id=f"l-{self.counter}")
        frame = await self.receive()
        assert frame["id"] == f"l-{self.counter}", frame
        if code is not None:
            assert frame["error"]["code"] == code, frame
            return frame["error"]
        assert "result" in frame, frame
        return frame["result"]

    async def start(self):
        await self.call("initialize", OFFER)


def stored(state, sid):
    directories = list(state.glob(f"*/{sid}"))
    assert len(directories) == 1 and directories[0].is_dir(), directories
    return directories[0]


def test_session_round_trip_and_single_owner(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        async with raw_runner(tmp_path, installed) as (_, send, receive):
            client = Client(send, receive)
            await client.start()
            params = session_params(raw_lease(tmp_path))
            sid = (await client.call("session/new", params))["session_id"]
            assert isinstance(sid, str) and sid
            await client.call("session/new", params, code=-32002)
            assert (await client.call("runner/health"))["sessions"] == 1
            assert await client.call("session/close", {"session_id": sid}) == {}
            assert (await client.call("runner/health"))["sessions"] == 0
            loaded = await client.call("session/load", {**params, "session_id": sid})
            assert loaded == {"session_id": sid, "resumed": True}
            await client.call("session/load", {**params, "session_key": "other", "session_id": sid}, code=-32002)
            assert (await client.call("runner/health"))["sessions"] == 1
            await client.call("session/close", {"session_id": sid})
            await client.call("session/close", {"session_id": sid}, code=-32001)
    run(exercise())


def test_saved_session_resumes_on_another_live_pool_member(tmp_path):
    async def exercise():
        async with dsh_supervisor(tmp_path) as supervisor:
            supervisor.session_cap = 1
            lease = await supervisor.acquire("dsh", owner="manager")
            params = session_params(lease, cwd=str(tmp_path))
            params["provider"]["reasoning"] = True
            params["limits"]["tool_output_bytes"] = 102400
            sid = await lease.open_session(params)
            directory = stored(dsh_command.state_root(tmp_path / "runtime"), sid)

            second = await supervisor.acquire("dsh")
            first_member, second_member = lease._generation, second._generation
            assert first_member is not second_member
            await lease.release()
            filler = await supervisor.acquire("dsh")
            assert filler._generation is first_member
            await second.release()

            resumed = await supervisor.acquire("dsh", owner="manager")
            assert resumed._generation is second_member
            assert resumed.state_dir == lease.state_dir
            assert first_member.process.returncode is second_member.process.returncode is None
            assert directory.is_dir()
            assert await resumed.load_session({**params, "session_id": sid}) == sid
            assert (await first_member.bridge.health())["sessions"] == 0
            assert (await second_member.bridge.health())["sessions"] == 1
            await resumed.release()
            await filler.release()
    run(exercise())


def test_load_refusals(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        async with raw_runner(tmp_path, installed) as (_, send, receive):
            client = Client(send, receive)
            await client.start()
            lease = raw_lease(tmp_path)
            params = session_params(lease)
            for value in (None, "", 1):
                await client.call("session/load", {**params, "session_id": value}, code=-32602)
            await client.call("session/load", {**params, "session_id": "unknown"}, code=-32001)
            sid = (await client.call("session/new", params))["session_id"]
            await client.call("session/close", {"session_id": sid})
            other = raw_lease(tmp_path, "other")
            await client.call("session/load", {**params, "cwd": str(other.directory), "session_id": sid}, code=-32001)
            assert await client.call("session/load", {**params, "session_id": sid}) == {
                "session_id": sid, "resumed": True}
            await client.call("session/close", {"session_id": sid})
            directory = stored(dsh_command.state_root(lease.runner_root), sid)
            logs = list(directory.glob("*.jsonl*"))
            assert logs, "positive control: the successful round trip must have created a log"
            for log in logs:
                log.write_bytes(b"not a valid saved session\n")
            error = await client.call("session/load", {**params, "session_id": sid}, code=-32001)
            assert error["message"] and "\n" not in error["message"]
            assert (await client.call("runner/health"))["sessions"] == 0
    run(exercise())


def test_session_new_refuses_before_sdk_work(tmp_path, monkeypatch):
    harness(tmp_path, monkeypatch, """rpc.method('test/block-sdk', () => {
  sdk.ctx.agents.create = () => { throw new Error('session SDK was touched'); };
  return {};
});
rpc.method('test/routes', () => ({routes: sdk.ctx.llm.listProviders().map(p => p.id)}));""")

    async def exercise():
        _, installed = require_dsh()
        async with raw_runner(tmp_path, installed) as (_, send, receive):
            client = Client(send, receive)
            await client.start()
            await client.call("test/block-sdk")
            lease = raw_lease(tmp_path)
            params = session_params(lease)
            file = lease.directory / "file"
            file.write_text("not a directory")
            link = lease.directory / "link"
            link.symlink_to(lease.home, target_is_directory=True)
            cases = [(override, -32602) for override in (
                {"session_key": "../bad"}, {"session_key": ""}, {"role": "consult"},
                *({field: value} for field in ("cwd", "home", "state_dir")
                    for value in ("relative", str(file), str(lease.directory / "missing"))),
                {"home": str(link)}, {"state_dir": str(link)}, {"add_dirs": ["relative"]},
                {"add_dirs": [str(file)]}, {"add_dirs": [str(lease.directory / "missing")]},
                *({"env": value} for value in (None, [], {"BAD-NAME": "x"}, {"OK": 1})),
                {"system_prompt": {"mode": "append", "text": 1}},
                *({"tools": {**params["tools"], "builtin": value}} for value in (None, [], "read", ["unknown"], [1])),
                *({"limits": value} for value in (None, [], {})),
                *({"limits": {**params["limits"], "tool_timeout_s": value}} for value in (0, 1.5, "120")),
                *({"limits": {**params["limits"], "max_requests_per_turn": value}} for value in (-1, 1.5, "30")),
            )]
            provider_cases = [{"protocol": "openai-responses"}, {"base_url": "not-an-endpoint"},
                *({field: ""} for field in ("base_url", "model", "api_key", "account_id")),
                *({"effort": value} for value in ("medium", 1)),
                *({"context_window": value} for value in (0, -1, 1.5, "1000"))]
            cases.extend(({"provider": {**params["provider"], **change}}, -32602) for change in provider_cases)
            cases.append(({"system_prompt": {"mode": "replace", "text": "x"}}, -32008))
            cases.extend(({"tools": {**params["tools"], "builtin": ["read", name]}}, -32008)
                for name in ("write", "edit", "bash"))
            cases.append(({"tools": {**params["tools"], "platform": [{"name": "tool"}]}}, -32007))
            cases.extend(({"fork_from": value}, -32008) for value in (None, {"session_id": "source"}))
            for override, code in cases:
                await client.call("session/new", {**params, **override}, code=code)
            assert (await client.call("runner/health"))["sessions"] == 0
            assert not (await client.call("test/routes"))["routes"]
            error = await client.call("session/new", params, code=-32603)
            assert error["message"] == "session SDK was touched"
            assert not (await client.call("test/routes"))["routes"]
            assert not list(dsh_command.state_root(lease.runner_root).glob("*/*"))
    run(exercise())


def test_capacity(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        async with raw_runner(tmp_path, installed) as (_, send, receive):
            client = Client(send, receive)
            await client.start()
            params = session_params(raw_lease(tmp_path))
            ids = [(await client.call("session/new", {**params, "session_key": f"key-{index}"}))["session_id"]
                for index in range(32)]
            assert (await client.call("runner/health"))["sessions"] == 32
            await client.call("session/new", {**params, "session_key": "overflow"}, code=-32006)
            await client.call("session/close", {"session_id": ids.pop()})
            ids.append((await client.call("session/new", {**params, "session_key": "overflow"}))["session_id"])
            for sid in ids:
                await client.call("session/close", {"session_id": sid})
    run(exercise())


def test_owner_session_survives_a_runner_kill(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        state = dsh_command.state_root(tmp_path / "shared-runtime")
        async with raw_runner(tmp_path, installed, state_root=state) as (process, send, receive):
            client = Client(send, receive)
            await client.start()
            params = session_params(raw_lease(tmp_path))
            sid = (await client.call("session/new", params))["session_id"]
            stored(state, sid)
            process.kill()
            assert await asyncio.wait_for(process.wait(), 5) < 0
        async with raw_runner(tmp_path, installed, state_root=state) as (_, send, receive):
            client = Client(send, receive)
            await client.start()
            assert await client.call("session/load", {**params, "session_id": sid}) == {
                "session_id": sid, "resumed": True}
            await client.call("session/close", {"session_id": sid})
    run(exercise())


def test_ephemeral_session_is_deleted_on_close(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        async with raw_runner(tmp_path, installed) as (_, send, receive):
            client = Client(send, receive)
            await client.start()
            lease = raw_lease(tmp_path)
            state = dsh_command.state_root(lease.runner_root)
            for ephemeral in (False, True):
                params = session_params(lease, state_dir=str(lease.native_state_dir if ephemeral else lease.state_dir))
                sid = (await client.call("session/new", params))["session_id"]
                directory = stored(state, sid)
                await client.call("session/close", {"session_id": sid})
                assert directory.exists() is not ephemeral
    run(exercise())


@pytest.mark.parametrize("end", ["eof", "sigterm", "closing-eof"])
def test_shutdown_closes_sessions_and_flushes_owner(tmp_path, monkeypatch, end):
    if end == "closing-eof":
        session_file = dsh_command.entry().resolve().parent / "session.ts"
        harness(tmp_path, monkeypatch, f"""const {{sessions}} = await import({json.dumps(session_file.as_uri())});
rpc.method('test/slow-close', p => {{
  const record = sessions.get(p.session_id), handle = record.handle;
  record.handle = {{async dispose() {{
    await rpc.notify('test/closing', {{}});
    await new Promise(resolve => setTimeout(resolve, 100));
    await handle.dispose();
  }}}};
  return {{}};
}});""")

    async def exercise():
        _, installed = require_dsh()
        state = dsh_command.state_root(tmp_path / "shared-runtime")
        async with raw_runner(tmp_path, installed, state_root=state) as (process, send, receive):
            client = Client(send, receive)
            await client.start()
            lease = raw_lease(tmp_path)
            params = session_params(lease)
            owner = (await client.call("session/new", params))["session_id"]
            ephemeral = (await client.call("session/new", {**params, "session_key": "ephemeral",
                "state_dir": str(lease.native_state_dir)}))["session_id"]
            owner_dir, ephemeral_dir = stored(state, owner), stored(state, ephemeral)
            if end == "closing-eof":
                await client.call("test/slow-close", {"session_id": ephemeral})
                await send("session/close", {"session_id": ephemeral}, id="l-99")
                assert (await receive())["method"] == "test/closing"
                process.stdin.close()
            elif end == "eof":
                process.stdin.close()
            else:
                process.send_signal(signal.SIGTERM)
            assert await asyncio.wait_for(process.wait(), 5) == 0
            assert owner_dir.is_dir() and not ephemeral_dir.exists()
        async with raw_runner(tmp_path, installed, state_root=state) as (_, send, receive):
            client = Client(send, receive)
            await client.start()
            assert await client.call("session/load", {**params, "session_id": owner}) == {
                "session_id": owner, "resumed": True}
            await client.call("session/close", {"session_id": owner})
    run(exercise())


def test_key_stays_in_memory(tmp_path):
    async def exercise():
        _, installed = require_dsh()
        async with raw_runner(tmp_path, installed) as (process, send, receive):
            client = Client(send, receive)
            await client.start()
            lease = raw_lease(tmp_path)
            params = session_params(lease)
            sid = (await client.call("session/new", params))["session_id"]
            # Both process argv and environment are covered, while its route is open.
            listing = subprocess.run(["ps", "eww", "-p", str(process.pid)], capture_output=True,
                text=True, check=True, timeout=5).stdout
            assert str(process.pid) in listing and KEY not in listing
            await client.call("session/close", {"session_id": sid})
            await client.call("session/load", {**params, "session_id": sid})
            await client.call("session/close", {"session_id": sid})
            process.stdin.close()
            assert await asyncio.wait_for(process.wait(), 5) == 0
            assert KEY.encode() not in await process.stderr.read()
            files = [path for path in lease.runner_root.rglob("*") if path.is_file()]
            assert any(path.suffix == ".json" for path in files), "positive control: inspect generated files"
            assert any(".jsonl" in path.name for path in files)
            for path in files:
                assert KEY.encode() not in path.read_bytes(), path
    run(exercise())


def test_agent_scope_prompt_tools_and_routes(tmp_path, monkeypatch):
    session_file = dsh_command.entry().resolve().parent / "session.ts"
    harness(tmp_path, monkeypatch, f"""const {{sessions}} = await import({json.dumps(session_file.as_uri())});
rpc.method('test/inspect', async p => {{
  const r = sessions.get(p.session_id);
  const assembly = await r.agentCtx.systemPrompt.assemble({{scope: r.agent}});
  return {{sections: assembly.sections, tools: r.agentCtx.tools.schemas(r.agent).map(t => t.name).sort(),
    model: r.model, wireModel: r.wireModel, route: r.route,
    models: await sdk.ctx.llm.listModels(r.route), routes: sdk.ctx.llm.listProviders().map(p => p.id).sort()}};
}});
rpc.method('test/tool', async p => {{
  const r = sessions.get(p.session_id);
  return await r.agentCtx.tools.execute({{agent: r.agent, name: p.name, arguments: p.arguments,
    callId: 'test-tool', signal: new AbortController().signal}});
}});""")

    async def exercise():
        _, installed = require_dsh()
        async with raw_runner(tmp_path, installed) as (_, send, receive):
            client = Client(send, receive)
            await client.start()
            writer, reader = raw_lease(tmp_path, "writer"), raw_lease(tmp_path, "reader")
            writable = {"builtin": ["read", "write", "edit", "bash"], "platform": [], "read_only": False}
            first = session_params(writer, tools=writable)
            second = session_params(reader, system_prompt={"mode": "append", "text": "Reader private section."})
            second["provider"]["api_key"] = "another-synthetic-key"
            first_id = (await client.call("session/new", first))["session_id"]
            second_id = (await client.call("session/new", second))["session_id"]
            # bash is the session-scoped replacement for the disabled native plugin,
            # so a writable builtin that names it is model-visible. The reader has no bash.
            for sid, params, names in ((first_id, first, ["bash", "edit", "read", "write"]),
                    (second_id, second, ["glob", "grep", "read"])):
                result = await client.call("test/inspect", {"session_id": sid})
                sections = [section for section in result["sections"] if section["name"] == "looper"]
                assert sections == [{"name": "looper", "text": params["system_prompt"]["text"], "interpolate": False}]
                assert result["tools"] == names
                assert result["routes"] == ["looper-reader", "looper-writer"]
                assert result["route"] == "looper-" + params["session_key"]
                assert result["model"] == "deepseek-v4-pro[1m]" and result["wireModel"] == "deepseek-v4-pro"
                assert [model["id"] for model in result["models"]] == ["deepseek-v4-pro"]
            canary = writer.directory / "canary.txt"
            mutation = {"name": "write", "arguments": {"file_path": str(canary), "content": "written"}}
            result = await client.call("test/tool", {"session_id": first_id, **mutation})
            assert not result.get("isError") and canary.read_text() == "written", result
            canary.unlink()
            denied = await client.call("test/tool", {"session_id": second_id, **mutation})
            assert denied["isError"] and "LOOPER_READ_ONLY" in json.dumps(denied)
            assert not canary.exists()
            denied = await client.call("test/tool", {"session_id": first_id, "name": "todo_write", "arguments": {}})
            assert denied["isError"] and "LOOPER_TOOL" in json.dumps(denied)
            await client.call("session/close", {"session_id": first_id})
            result = await client.call("test/inspect", {"session_id": second_id})
            assert result["routes"] == ["looper-reader"]
            await client.call("session/close", {"session_id": second_id})
    run(exercise())


def test_setup_failure_releases_route_agent_and_storage(tmp_path, monkeypatch):
    harness(tmp_path, monkeypatch, """rpc.method('test/fail-setup', () => {
  const create = sdk.ctx.agents.create.bind(sdk.ctx.agents);
  sdk.ctx.agents.create = options => create({...options, setup: (...args) => {
    options.setup(...args);
    throw new Error('synthetic setup failure');
  }});
  return {};
});
rpc.method('test/routes', () => ({routes: sdk.ctx.llm.listProviders().map(p => p.id), agents: sdk.ctx.agents.list().length}));""")

    async def exercise():
        _, installed = require_dsh()
        async with raw_runner(tmp_path, installed) as (_, send, receive):
            client = Client(send, receive)
            await client.start()
            lease = raw_lease(tmp_path)
            await client.call("test/fail-setup")
            error = await client.call("session/new", session_params(lease), code=-32603)
            assert error["message"] == "synthetic setup failure"
            assert (await client.call("runner/health"))["sessions"] == 0
            assert await client.call("test/routes") == {"routes": [], "agents": 0}
            assert not list(dsh_command.state_root(lease.runner_root).glob("*/*"))
    run(exercise())
