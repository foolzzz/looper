"""Run roles and read-only history forks through the installed Pi SDK wire."""
import asyncio
import hashlib
import json
import shutil

import pytest

from fake_model_api import FakeModelAPI, ResponseScript, call_events, event, text_events, text_item
from pi_runner_support import raw_lease, raw_runner, require_pi, session_params

pytestmark = pytest.mark.installed
KEY = "run-session-test-key"
OFFER = {"protocol_versions": ["0.1"], "looper_version": "test", "host_capabilities": {
    "tools_call": True, "budget_authorize": True, "bus": False, "exec_slots": False}}
WRITABLE = {"builtin": ["read", "bash", "edit", "write"], "platform": [], "read_only": False}
READ_ONLY = {"builtin": ["read", "grep", "find", "ls"], "platform": [], "read_only": True}


def run(coroutine):
    asyncio.run(asyncio.wait_for(coroutine, 30))


async def turn(send, receive, sid, trace):
    binding = {"session_id": sid, "trace_id": trace}
    await send("session/prompt", {**binding, "text": "Continue", "tools": []}, id=f"l-{trace}")
    notices = []
    while not notices or notices[-1]["kind"] != "settled":
        frame = await receive()
        if frame.get("method") == "budget/authorize":
            await send({"jsonrpc": "2.0", "id": frame["id"], "result": {**binding, "allow": True}})
        elif frame.get("method") == "session/update":
            notices.append(frame["params"])
        else:
            assert frame["result"]["accepted"]
    assert notices[-1]["outcome"] == "completed"
    return notices


def test_run_roles_open_with_their_tool_sets(tmp_path):
    async def exercise():
        _, pi = require_pi()
        scripts = [ResponseScript(events=text_events()) for _ in range(2)]
        async with FakeModelAPI(scripts, api_key=KEY) as api, raw_runner(tmp_path, pi) as (_, send, receive):
            await send("initialize", OFFER)
            assert (await receive())["result"]["capabilities"]["fork"] is True
            for index, role in enumerate(("worker", "reviewer")):
                lease = raw_lease(tmp_path, role)
                params = session_params(lease, base_url=api.base_url, key=KEY, role=role, tools=WRITABLE)
                await send("session/new", params, id=f"l-{index * 3 + 2}")
                sid = (await receive())["result"]["session_id"]
                await turn(send, receive, sid, str(index * 3 + 3))
                assert {tool["name"] for tool in api.requests[index].body["tools"]} == set(WRITABLE["builtin"])
                await send("session/close", {"session_id": sid}, id=f"l-{index * 3 + 4}")
                assert (await receive())["result"] == {}
            await send("session/new", {**params, "role": "consult"}, id="l-8")
            assert (await receive())["error"]["code"] == -32602
    run(exercise())


@pytest.mark.parametrize("timeout", [0, 1.5, "120"])
def test_session_limits_are_validated(tmp_path, timeout):
    async def exercise():
        _, pi = require_pi()
        async with raw_runner(tmp_path, pi) as (_, send, receive):
            lease = raw_lease(tmp_path)
            params = session_params(lease, base_url="http://127.0.0.1:1/v1", key=KEY,
                limits={"tool_timeout_s": timeout, "max_requests_per_turn": 30})
            await send("initialize", OFFER)
            assert "result" in await receive()
            await send("session/new", params, id="l-2")
            assert (await receive())["error"]["code"] == -32602
            await send("runner/health", id="l-3")
            assert (await receive())["result"]["sessions"] == 0
            assert not list(lease.state_dir.glob("*.jsonl"))
    run(exercise())


def test_fork_continues_a_copy_without_writing_it(tmp_path):
    async def exercise():
        _, pi = require_pi()
        sentinel = "FORK-HISTORY-SENTINEL"
        outside = tmp_path / "outside.txt"
        outside.write_text("PRIVATE-OUTSIDE-CONTENT")
        item = text_item(sentinel)
        scripts = [ResponseScript(events=[
            event("response.output_item.added", output_index=0, item={**item, "content": []}),
            *text_events(sentinel)[:-1], event("response.output_item.done", output_index=0, item=item),
            text_events(sentinel)[-1]]),
            ResponseScript(events=call_events(json.dumps({"path": str(outside)}), name="read")),
            ResponseScript(events=text_events("done"))]
        async with FakeModelAPI(scripts, api_key=KEY) as api, raw_runner(tmp_path, pi) as (_, send, receive):
            source = raw_lease(tmp_path, "source")
            await send("initialize", OFFER)
            assert "result" in await receive()
            await send("session/new", session_params(source, base_url=api.base_url, key=KEY,
                role="worker", tools=WRITABLE), id="l-2")
            original_id = (await receive())["result"]["session_id"]
            await turn(send, receive, original_id, "3")
            await send("session/close", {"session_id": original_id}, id="l-4")
            assert (await receive())["result"] == {}
            original, = source.state_dir.glob("*.jsonl")
            assert sentinel in original.read_text()
            fork = raw_lease(tmp_path, "fork")
            copy = fork.state_dir / "source.jsonl"
            shutil.copyfile(original, copy)
            copy.chmod(0o400)
            untouched = fork.state_dir / "unrelated.txt"
            untouched.write_text("leave this file alone")
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (original, *fork.state_dir.iterdir())}
            await send("session/new", session_params(fork, base_url=api.base_url, key=KEY,
                role="reviewer", tools=READ_ONLY, fork_from={"session_id": original_id}), id="l-5")
            sid = (await receive())["result"]["session_id"]
            assert sid != original_id
            notices = await turn(send, receive, sid, "6")
            assert sentinel in json.dumps(api.requests[1].body["input"])
            for request in api.requests[1:]:
                assert sorted(tool["name"] for tool in request.body["tools"]) == sorted(READ_ONLY["builtin"])
            result, = [notice for notice in notices if notice["kind"] == "tool_result"]
            assert result["is_error"] and "Looper blocked read" in result["text"]
            assert "PRIVATE-OUTSIDE-CONTENT" not in json.dumps(api.requests[2].body)
            await send("session/close", {"session_id": sid}, id="l-7")
            assert (await receive())["result"] == {}
            created = set(fork.state_dir.iterdir()) - {copy, untouched}
            new_file, = created
            assert new_file.suffix == ".jsonl" and json.loads(new_file.read_text().splitlines()[0])["id"] == sid
            assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before} == before
            assert copy.stat().st_mode & 0o777 == 0o400
    run(exercise())


@pytest.mark.parametrize("case,code", [("missing", -32001), ("ambiguous", -32001), ("load", -32602)])
def test_fork_refusals(tmp_path, case, code):
    async def exercise():
        _, pi = require_pi()
        async with raw_runner(tmp_path, pi) as (_, send, receive):
            lease = raw_lease(tmp_path)
            params = session_params(lease, base_url="http://127.0.0.1:1/v1", key=KEY, role="reviewer",
                fork_from={"session_id": "source"})
            if case == "ambiguous":
                for name in ("first", "second"):
                    (lease.state_dir / f"{name}.jsonl").write_text('{"type":"session","id":"source"}\n')
            before = {p: p.read_bytes() for p in lease.state_dir.iterdir()}
            await send("initialize", OFFER)
            assert "result" in await receive()
            await send("session/load" if case == "load" else "session/new", {**params, "session_id": "source"}, id="l-2")
            assert (await receive())["error"]["code"] == code
            await send("runner/health", id="l-3")
            assert (await receive())["result"]["sessions"] == 0
            assert {p: p.read_bytes() for p in lease.state_dir.iterdir()} == before
            # The rejected request must not retain the key's reservation.
            del params["fork_from"]
            await send("session/new", params, id="l-4")
            sid = (await receive())["result"]["session_id"]
            await send("session/close", {"session_id": sid}, id="l-5")
            assert (await receive())["result"] == {}
    run(exercise())


def test_invalid_parameters_are_refused_before_session_sdk_work(tmp_path, monkeypatch):
    from looper.agents.runtimes.runner import pi_command

    main = pi_command.entry().resolve()
    harness = tmp_path / "guard-sdk.ts"
    harness.write_text(f'''import * as rpc from {json.dumps((main.parent / 'rpc.ts').as_uri())};
import {{sdk}} from {json.dumps((main.parent / 'sdk.ts').as_uri())};
rpc.method('test/block-sdk', () => {{
  sdk.agent.ModelRuntime.create = () => {{ throw new Error('session SDK was touched'); }};
  return {{}};
}});
await import({json.dumps(main.as_uri())});
''')
    monkeypatch.setattr(pi_command, "entry", lambda: harness)

    async def exercise():
        _, pi = require_pi()
        async with raw_runner(tmp_path, pi) as (_, send, receive):
            params = session_params(raw_lease(tmp_path), base_url="http://127.0.0.1:1/v1", key=KEY)
            await send("initialize", OFFER)
            assert "result" in await receive()
            await send("test/block-sdk", id="l-2")
            assert (await receive())["result"] == {}
            cases = [{"role": "consult"}, *({"limits": value} for value in (None, [], {})),
                *({"limits": {"tool_timeout_s": value, "max_requests_per_turn": 30}} for value in (0, 1.5, "120")),
                *({"limits": {"tool_timeout_s": 120, "max_requests_per_turn": value}} for value in (-1, 1.5, "30")),
                *({"fork_from": value} for value in (None, [], "source", {}, {"session_id": ""}, {"session_id": 1}))]
            for index, override in enumerate(cases, 3):
                await send("session/new", {**params, **override}, id=f"l-{index}")
                assert (await receive())["error"]["code"] == -32602, override
            await send("session/load", {**params, "session_id": "source", "fork_from": {"session_id": "source"}}, id="l-30")
            assert (await receive())["error"]["code"] == -32602
            await send("runner/health", id="l-31")
            assert (await receive())["result"]["sessions"] == 0
            # Positive control: a valid request reaches the guarded SDK boundary.
            await send("session/new", params, id="l-32")
            error = (await receive())["error"]
            assert error["code"] == -32603 and error["message"] == "session SDK was touched"
    run(exercise())


def test_fork_close_right_after_prompt_settles_once(tmp_path):
    async def exercise():
        _, pi = require_pi()
        async with FakeModelAPI([], api_key=KEY) as api, raw_runner(tmp_path, pi) as (process, send, receive):
            lease = raw_lease(tmp_path)
            original_id = "00000000-0000-4000-8000-000000000001"
            (lease.state_dir / "source.jsonl").write_text(json.dumps({"type": "session", "version": 3,
                "id": original_id, "timestamp": "2026-01-01T00:00:00.000Z", "cwd": str(lease.directory)}) + "\n")
            await send("initialize", OFFER)
            assert "result" in await receive()
            await send("session/new", session_params(lease, base_url=api.base_url, key=KEY,
                role="reviewer", tools=READ_ONLY, fork_from={"session_id": original_id}), id="l-2")
            sid = (await receive())["result"]["session_id"]
            assert sid != original_id
            binding = {"session_id": sid, "trace_id": "close"}
            messages = [{"jsonrpc": "2.0", "id": "l-3", "method": "session/prompt",
                "params": {**binding, "text": "Continue", "tools": []}},
                {"jsonrpc": "2.0", "id": "l-4", "method": "session/close", "params": binding}]
            process.stdin.write(b"".join(json.dumps(message).encode() + b"\n" for message in messages))
            await process.stdin.drain()
            accepted, settled, closed = [await receive() for _ in range(3)]
            assert accepted["result"]["accepted"]
            assert settled["params"]["kind"] == "settled" and settled["params"]["outcome"] == "cancelled"
            assert settled["params"]["requests"] == 0
            assert closed["result"] == binding
            await send("runner/health", id="l-5")
            assert (await receive())["result"]["sessions"] == 0
            assert not api.requests
    run(exercise())
