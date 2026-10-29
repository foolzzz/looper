"""Platform snapshots and in-trace steering through the installed Pi SDK."""
import asyncio
import json
import subprocess

import pytest

from fake_model_api import FakeModelAPI, ResponseScript, call_events, completed, event, text_item
from looper.agents.runtimes.api.loop import ToolOutcome, ToolSpec
from pi_runner_support import TEST_TIMEOUT, bind_pi_turn, pi_session, require_pi

pytestmark = pytest.mark.installed
KEY = "!literal-platform-secret"
USAGE = {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}
STEER = "Include the newly supplied task requirement."


def run(coroutine):
    asyncio.run(asyncio.wait_for(coroutine, TEST_TIMEOUT))


def tool_reply(name="looper_tasks", call_id="call_tasks"):
    events = call_events('{"action":"list"}', name=name, call_id=call_id, id="fc_tasks")
    events[-1]["response"]["usage"] = USAGE
    return ResponseScript(events=events)


def answer(text="Done", *, gate=None):
    item = text_item(text)
    return ResponseScript(events=[
        event("response.output_item.added", output_index=0, item={**item, "content": []}),
        event("response.content_part.added", output_index=0, content_index=0,
            part={"type": "output_text", "text": "", "annotations": []}),
        event("response.output_text.delta", output_index=0, item_id=item["id"], content_index=0, delta=text),
        event("response.output_item.done", output_index=0, item=item), completed([item], usage=USAGE)], gate=gate)


class Tools:
    def __init__(self, text="task fact", *, is_error=False, gate=None, names=("looper_tasks",)):
        self.text, self.is_error, self.gate, self.names = text, is_error, gate, names
        self.calls = []
        self.description, self.schema = "Inspect platform state", {}
        self.entered, self.finished = asyncio.Event(), asyncio.Event()

    async def specs(self):
        # The runner supplies absent object type/properties for the Pi SDK.
        return [ToolSpec(name, self.description, self.schema) for name in self.names]

    async def call(self, call):
        self.calls.append(call)
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        self.finished.set()
        return ToolOutcome(self.text, self.is_error, ran=True)


def steer_events(events):
    return [e for e in events if e.get("subtype") in {"steer_applied", "steer_dropped"}]


def observe_settled(turn):
    """Observe runner settlement before the host drains admitted tool writes."""
    received = asyncio.Event()
    original = turn.finish

    async def finish(params):
        received.set()
        return await original(params)

    turn.finish = finish
    return received


def outputs(request):
    return [item for item in request.body["input"] if item.get("type") == "function_call_output"]


@pytest.mark.parametrize("text,is_error", [("task fact\nwith exact whitespace  ", False),
    ("é" * 51200, False), ("business operation rejected", True)], ids=["short", "100-kib", "business-error"])
def test_platform_tool_round_trip(tmp_path, text, is_error):
    async def exercise():
        tools = Tools(text, is_error=is_error)
        # Discovered extensions must remain disabled while the inline proxy works.
        extension = tmp_path / ".pi/extensions/leak.js"
        extension.parent.mkdir(parents=True)
        marker = tmp_path / "extension-loaded"
        extension.write_text("import {writeFileSync} from 'node:fs';\n"
            f"export default function () {{ writeFileSync({json.dumps(str(marker))}, 'loaded'); }}\n")
        async with FakeModelAPI([tool_reply(), answer()], api_key=KEY) as api, pi_session(tmp_path, api, tools) as session:
            events = []
            result = await session.run_turn("Inspect tasks", events.append)
            assert result.result_event["runner"]["outcome"] == "completed"
            assert len(tools.calls) == 1
            call = tools.calls[0]
            assistant = next(e for e in events if e["type"] == "assistant")
            tool_use, = assistant["message"]["content"]
            assert call.call_id == tool_use["id"] == "call_tasks|fc_tasks"
            assert call.name == "looper_tasks"
            assert json.loads(call.arguments) == {"action": "list"}
            assert len(api.requests) == 2
            assert outputs(api.requests[1]) == [{"type": "function_call_output", "call_id": "call_tasks", "output": text}]
            spec, = api.requests[0].body["tools"]
            assert spec["name"] == "looper_tasks" and spec["strict"] is False
            assert spec["parameters"]["type"] == "object" and spec["parameters"]["properties"] == {}
            tool_result = next(e for e in events if e["type"] == "user")["message"]["content"][0]
            assert tool_result["content"] == text and tool_result["is_error"] is is_error
            native = [json.loads(line) for file in session._lease.state_dir.glob("*.jsonl") for line in file.read_text().splitlines()]
            message, = [row["message"] for row in native if row.get("type") == "message" and row["message"]["role"] == "toolResult"]
            assert message["isError"] is is_error
            assert not marker.exists()
            # Positive control: the extension has a working, observable entry point.
            node, _ = require_pi()
            subprocess.run([node, "--input-type=module", "-e",
                f"import extension from {json.dumps(extension.as_uri())}; extension();"],
                check=True, capture_output=True, timeout=5)
            assert marker.read_text() == "loaded"
    run(exercise())


def test_tool_snapshot_follows_each_prompt(tmp_path):
    async def exercise():
        tools = Tools(names=("looper_tasks", "looper_projects"))
        async with FakeModelAPI([answer(), tool_reply("looper_projects"), answer(), tool_reply("looper_projects"), answer()], api_key=KEY) as api, pi_session(tmp_path, api, tools) as session:
            assert not (await session.run_turn("First snapshot", [].append)).result_event["is_error"]
            tools.names = ("looper_tasks",)
            bind_pi_turn(session, tools)
            events = []
            result = await session.run_turn("Second snapshot", events.append)
            assert not result.result_event["is_error"]
            assert {t["name"] for t in api.requests[0].body["tools"]} == {"looper_tasks", "looper_projects"}
            assert [t["name"] for t in api.requests[1].body["tools"]] == ["looper_tasks"]
            assert not tools.calls
            unknown, = outputs(api.requests[2])
            assert "looper_projects" in unknown["output"] and "not found" in unknown["output"].lower()
            tool_result = next(e for e in events if e["type"] == "user")["message"]["content"][0]
            assert tool_result["is_error"]
            tools.names = ("looper_tasks", "looper_projects")
            tools.description = "Updated platform schema"
            tools.schema = {"type": "object", "properties": {"action": {"type": "string"}}}
            bind_pi_turn(session, tools)
            assert not (await session.run_turn("Re-add with a changed schema", [].append)).result_event["is_error"]
            assert len(tools.calls) == 1 and tools.calls[0].name == "looper_projects"
            for spec in api.requests[3].body["tools"]:
                assert spec["description"] == tools.description and spec["parameters"] == tools.schema
    run(exercise())


def test_cancel_during_a_platform_tool(tmp_path):
    async def exercise():
        gate = asyncio.Event()
        tools = Tools(gate=gate)
        async with FakeModelAPI([tool_reply()], api_key=KEY) as api, pi_session(tmp_path, api, tools) as session:
            events = []
            task = asyncio.create_task(session.run_turn("Inspect tasks", events.append))
            try:
                await tools.entered.wait()
                turn, lease = session._turn, session._lease
                received = observe_settled(turn)
                await lease.bridge.cancel_turn(turn.session_id, turn.trace_id)
                await asyncio.wait_for(received.wait(), 1)
                assert not gate.is_set() and not tools.finished.is_set() and not task.done()
            finally:
                gate.set()
                result = await task
            assert result.result_event["runner"]["outcome"] == "cancelled"
            assert len(tools.calls) == 1 and tools.finished.is_set() and len(api.requests) == 1
            assert (await lease.bridge.health())["running"] == 0
    run(exercise())


def test_steer_applies_in_the_same_trace(tmp_path):
    async def exercise():
        gate = asyncio.Event()
        tools = Tools(gate=gate)
        async with FakeModelAPI([tool_reply(), answer()], api_key=KEY) as api, pi_session(tmp_path, api, tools) as session:
            events = []
            task = asyncio.create_task(session.run_turn("Inspect tasks", events.append))
            try:
                await tools.entered.wait()
                queued = await session.steer(STEER, message_id="m1")
                assert queued["status"] == "queued"
                assert await session.steer(STEER, message_id="m1") == queued
            finally:
                gate.set()
                result = await task
            assert not result.result_event["is_error"]
            assert {e["trace_id"] for e in events} == {queued["trace_id"]}
            assert len([e for e in events if e["type"] == "result"]) == 1
            applied, = steer_events(events)
            assert applied["subtype"] == "steer_applied"
            assert applied["runner"]["message_id"] == "m1"
            assert applied["runner"]["after_tool_batch"] is True and applied["runner"]["new_turn"] is False
            first_request = next(e for e in events if e["type"] == "usage")["request_id"]
            assert applied["runner"]["after_request_id"] == first_request
            assert json.dumps(api.requests[1].body["input"]).count(STEER) == 1
            assert events.index(applied) < next(i for i, e in enumerate(events) if e["type"] == "result")
    run(exercise())


@pytest.mark.parametrize("decision", ["allow", "max_requests", "run_cap", "stopped", "cancel"])
def test_steer_waits_for_request_send_and_cannot_leak_after_denial(tmp_path, monkeypatch, decision):
    from looper.agents.runtimes.runner.turn import RunnerTurn

    async def exercise():
        gate, authorizing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        tools = Tools(gate=gate)
        original = RunnerTurn.authorize
        attempts = 0

        async def held(turn, params):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                authorizing.set()
                await release.wait()
                if decision == "max_requests":
                    turn.max_requests = 1
                elif decision == "run_cap":
                    async def limits():
                        return dict(run_cap=0, task_cap=None, task_spent=0)
                    turn._limits = limits
                elif decision == "stopped":
                    turn.stop()
            return await original(turn, params)

        monkeypatch.setattr(RunnerTurn, "authorize", held)
        async with FakeModelAPI([tool_reply(), answer(), answer("Next clean turn")], api_key=KEY) as api, pi_session(tmp_path, api, tools) as session:
            events = []
            task = asyncio.create_task(session.run_turn("Inspect tasks", events.append))
            try:
                await tools.entered.wait()
                queued = await session.steer(STEER, message_id="m1")
                gate.set()
                await authorizing.wait()
                assert len(api.requests) == 1
                # SDK history insertion has happened, but the request is still held.
                assert not steer_events(events)
                if decision == "cancel":
                    turn, lease = session._turn, session._lease
                    await lease.bridge.cancel_turn(turn.session_id, turn.trace_id)
            finally:
                gate.set()
                release.set()
                result = await task
            notice, = steer_events(events)
            assert notice["trace_id"] == queued["trace_id"]
            assert notice["runner"]["message_id"] == "m1"
            assert notice["subtype"] == ("steer_applied" if decision == "allow" else "steer_dropped")
            assert result.result_event["runner"]["outcome"] == {
                "allow": "completed", "max_requests": "max_requests", "run_cap": "capped",
                "stopped": "cancelled", "cancel": "cancelled"}[decision]
            assert len(api.requests) == (2 if decision == "allow" else 1)
            if decision == "allow":
                assert json.dumps(api.requests[1].body).count(STEER) == 1
            else:
                native = [json.loads(line) for file in session._lease.state_dir.glob("*.jsonl")
                    for line in file.read_text().splitlines()]
                steered, = [row for row in native if row.get("type") == "message"
                    and row["message"]["role"] == "user" and STEER in json.dumps(row["message"]["content"])]
                assert any(row.get("type") == "context_edit" and row["targetId"] == steered["id"]
                    and row["replacement"] is None for row in native)
            assert events.index(notice) < next(i for i, e in enumerate(events) if e["type"] == "result")
            bind_pi_turn(session, tools)
            assert not (await session.run_turn("Next turn", [].append)).result_event["is_error"]
            next_body = json.dumps(api.requests[-1].body)
            assert "Inspect tasks" in next_body  # Cleanup must preserve the original history.
            if decision != "allow":
                assert STEER not in next_body
    run(exercise())


@pytest.mark.parametrize("ending", ["cancel", "close"])
def test_dropped_steer_is_omitted_after_aborted_compaction(tmp_path, ending):
    async def exercise():
        tool_gate, summary_gate = asyncio.Event(), asyncio.Event()
        tools = Tools("x" * 100000, gate=tool_gate)
        summary = answer("Checkpoint", gate=summary_gate)
        scripts = [tool_reply(), summary, answer("Clean checkpoint"), answer("Next clean turn")]
        async with FakeModelAPI(scripts, api_key=KEY) as api, pi_session(
            tmp_path, api, tools, context_window=40000
        ) as session:
            events = []
            task = asyncio.create_task(session.run_turn("Inspect tasks", events.append))
            closing = None
            try:
                await tools.entered.wait()
                queued = await session.steer(STEER, message_id="m1")
                tool_gate.set()
                await summary.gate_reached.wait()
                assert len(api.requests) == 2
                assert "summar" in api.requests[1].body["instructions"].lower()
                native = [json.loads(line) for file in session._lease.state_dir.glob("*.jsonl")
                    for line in file.read_text().splitlines()]
                assert any(row.get("type") == "message" and row["message"]["role"] == "toolResult"
                    for row in native)
                assert STEER not in json.dumps(native)  # Drained, but not appended yet.
                turn, lease = session._turn, session._lease
                if ending == "close":
                    closing = asyncio.create_task(lease.bridge.close_session(turn.session_id))
                else:
                    await lease.bridge.cancel_turn(turn.session_id, turn.trace_id)
                result = await task  # Abort must unblock compaction without opening its gate.
                assert not summary_gate.is_set()
            finally:
                tool_gate.set()
                summary_gate.set()
                await task
                if closing is not None:
                    await closing
            assert result.result_event["runner"]["outcome"] == "cancelled"
            notice, = steer_events(events)
            assert notice["trace_id"] == queued["trace_id"]
            assert notice["subtype"] == "steer_dropped" and notice["runner"]["message_id"] == "m1"
            assert notice["runner"]["reason"] == ("session_closed" if ending == "close" else "cancelled")
            assert events.index(notice) < next(i for i, e in enumerate(events) if e["type"] == "result")
            native = [json.loads(line) for file in lease.state_dir.glob("*.jsonl")
                for line in file.read_text().splitlines()]
            steered, = [row for row in native if row.get("type") == "message"
                and row["message"]["role"] == "user" and STEER in json.dumps(row["message"]["content"])]
            edit, = [row for row in native if row.get("type") == "context_edit"
                and row["targetId"] == steered["id"] and row["replacement"] is None]
            assert native.index(edit) > native.index(steered)
            if ending == "close":
                assert (await lease.bridge.health())["sessions"] == 0
                return
            bind_pi_turn(session, tools)
            assert not (await session.run_turn("Next turn", [].append)).result_event["is_error"]
            assert len(api.requests) > 2
            assert all(STEER not in json.dumps(request.body) for request in api.requests[2:])
            assert any("Inspect tasks" in json.dumps(request.body) for request in api.requests[2:])
    run(exercise())


@pytest.mark.parametrize("ending", ["cancel", "close", "final-answer"])
def test_steer_is_dropped_never_lost(tmp_path, ending):
    async def exercise():
        gate = asyncio.Event()
        tools = Tools(gate=gate if ending != "final-answer" else None)
        first = tool_reply() if ending != "final-answer" else answer("Final answer", gate=gate)
        scripts = [first] if ending != "final-answer" else [first, answer("Steered answer")]
        async with FakeModelAPI(scripts, api_key=KEY) as api, pi_session(tmp_path, api, tools) as session:
            events = []
            task = asyncio.create_task(session.run_turn("First turn", events.append))
            closing = None
            try:
                await (tools.entered.wait() if ending != "final-answer" else first.gate_reached.wait())
                queued = await session.steer(STEER, message_id="m1")
                assert await session.steer(STEER, message_id="m1") == queued
                if ending != "final-answer":
                    turn, lease = session._turn, session._lease
                    received = observe_settled(turn)
                    if ending == "close":
                        closing = asyncio.create_task(lease.bridge.close_session(turn.session_id))
                    else:
                        await lease.bridge.cancel_turn(turn.session_id, turn.trace_id)
                    await asyncio.wait_for(received.wait(), 1)
                gate.set()
            finally:
                gate.set()
                await task
                if closing is not None:
                    await closing
            notice, = steer_events(events)
            assert notice["runner"]["message_id"] == "m1"
            if ending != "final-answer":
                assert notice["subtype"] == "steer_dropped"
                assert notice["runner"]["reason"] == ("session_closed" if ending == "close" else "cancelled")
            else:
                assert notice["subtype"] in {"steer_applied", "steer_dropped"}
                if notice["subtype"] == "steer_dropped":
                    assert notice["runner"]["reason"] == "turn_ended"
                else:
                    ended = [e["request_id"] for e in events[:events.index(notice)] if e["type"] == "usage"]
                    assert notice["runner"]["after_request_id"] == (ended[-1] if ended else None)
            assert events.index(notice) < next(i for i, e in enumerate(events) if e["type"] == "result")
            if ending == "close":
                assert (await lease.bridge.health())["sessions"] == 0
                return
            api.scripts.append(answer("Next clean turn"))
            bind_pi_turn(session, tools)
            assert not (await session.run_turn("Next turn", [].append)).result_event["is_error"]
            # History may contain an applied steer; no queued steer may enter this request.
            next_users = [item for item in api.requests[-1].body["input"] if item.get("role") == "user"]
            assert STEER not in json.dumps(next_users[-1])
            if notice["subtype"] == "steer_dropped":
                assert STEER not in json.dumps(api.requests[-1].body)
    run(exercise())


@pytest.mark.parametrize("snapshot", [
    [{"name": "bad.name", "description": "d", "inputSchema": {}}],
    [{"name": "read", "description": "d", "inputSchema": {}}],
    [{"name": "looper_tasks", "description": "d", "inputSchema": {"type": "array"}}],
    [{"name": "looper_tasks", "description": "d", "inputSchema": {}}] * 2,
], ids=["name", "builtin", "type", "duplicate"])
def test_invalid_snapshot_creates_no_turn(tmp_path, snapshot):
    from looper.agents.runtimes.runner.bridge import RpcFailure

    async def exercise():
        async with FakeModelAPI([answer()], api_key=KEY) as api, pi_session(tmp_path, api, Tools(names=())) as session:
            lease = session._lease
            with pytest.raises(RpcFailure) as error:
                await lease.bridge.request("session/prompt", {"session_id": lease.session_id,
                    "trace_id": "invalid", "text": "Hello", "tools": snapshot})
            assert error.value.code == -32602
            assert (await lease.bridge.health())["running"] == 0 and not api.requests
            assert not (await session.run_turn("Valid prompt after rejection", [].append)).result_event["is_error"]
            assert len(api.requests) == 1
    run(exercise())


def test_steer_before_first_request_and_control_errors(tmp_path, monkeypatch):
    from looper.agents.runtimes.runner.bridge import RpcFailure
    from looper.agents.runtimes.runner.turn import RunnerTurn

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        original = RunnerTurn.authorize

        async def held(self, params):
            entered.set()
            await release.wait()
            return await original(self, params)

        monkeypatch.setattr(RunnerTurn, "authorize", held)
        async with FakeModelAPI([answer(), answer("Steered answer")], api_key=KEY) as api, pi_session(tmp_path, api, Tools(names=())) as session:
            bridge, sid = session._lease.bridge, session._lease.session_id
            with pytest.raises(RpcFailure) as error:
                await bridge.request("session/steer", {"session_id": sid, "trace_id": "idle",
                    "message_id": "idle", "text": STEER, "if_idle": "reject"})
            assert error.value.code == -32004
            events = []
            task = asyncio.create_task(session.run_turn("First turn", events.append))
            try:
                await entered.wait()
                assert not api.requests
                with pytest.raises(RpcFailure) as error:
                    await bridge.request("session/steer", {"session_id": sid, "trace_id": "another-trace",
                        "message_id": "wrong-trace", "text": STEER, "if_idle": "reject"})
                assert error.value.code == -32005
                assert (await session.steer(STEER, message_id="early"))["status"] == "queued"
            finally:
                release.set()
                result = await task
            assert not result.result_event["is_error"]
            notice, = steer_events(events)
            assert notice["subtype"] == "steer_applied" and notice["runner"]["message_id"] == "early"
            assert any(STEER in json.dumps(request.body["input"]) for request in api.requests)
    run(exercise())


def test_platform_rpc_error_is_safe_and_explained(tmp_path):
    class BrokenTools(Tools):
        async def call(self, call):
            raise RuntimeError(f"Private callback detail: {KEY}")

    async def exercise():
        async with FakeModelAPI([tool_reply(), answer()], api_key=KEY) as api, pi_session(tmp_path, api, BrokenTools()) as session:
            events = []
            assert not (await session.run_turn("Inspect tasks", events.append)).result_event["is_error"]
            output, = outputs(api.requests[1])
            assert output["output"] == "Looper did not run the tool looper_tasks: runner host callback failed"
            tool_result = next(e for e in events if e["type"] == "user")["message"]["content"][0]
            assert tool_result["is_error"] is True
            assert KEY not in json.dumps(events)
            assert KEY not in "".join(file.read_text() for file in session._lease.state_dir.glob("*.jsonl"))
    run(exercise())


def test_steer_accepted_before_agent_start(tmp_path):
    from types import SimpleNamespace

    from pi_runner_support import raw_runner, session_params

    async def exercise():
        _, pi = require_pi()
        scripts = [answer(), answer("Steered answer")]
        async with FakeModelAPI(scripts, api_key=KEY) as api, raw_runner(tmp_path, pi) as (process, send, receive):
            root, = tmp_path.glob("pi-runner-*")
            directory = root / "sessions/early"
            for name in ("agent", "native", "home"):
                (directory / name).mkdir(parents=True)
            lease = SimpleNamespace(session_key="early", directory=directory,
                home=directory / "home", state_dir=directory / "native")
            await send("initialize", {"protocol_versions": ["0.1"], "looper_version": "test",
                "host_capabilities": {"tools_call": True, "budget_authorize": True, "bus": False, "exec_slots": False}})
            await receive()
            await send("session/new", session_params(lease, base_url=api.base_url, key=KEY), id="l-2")
            sid = (await receive())["result"]["session_id"]
            trace = {"session_id": sid, "trace_id": "early"}
            # Raw transport covers malformed values rejected by the host codec.
            for index, invalid in enumerate(({"description": 1}, {"inputSchema": []})):
                await send("session/prompt", {**trace, "text": "Invalid snapshot", "tools": [
                    {"name": "looper_tasks", "description": "Inspect", "inputSchema": {}, **invalid}]}, id=f"l-{10 + index}")
                assert (await receive())["error"]["code"] == -32602
            await send("runner/health", {}, id="l-12")
            assert (await receive())["result"]["running"] == 0 and not api.requests
            messages = [
                {"jsonrpc": "2.0", "id": "l-3", "method": "session/prompt",
                    "params": {**trace, "text": "First turn", "tools": []}},
                {"jsonrpc": "2.0", "id": "l-4", "method": "session/steer",
                    "params": {**trace, "message_id": "before-start", "text": STEER, "if_idle": "reject"}},
            ]
            process.stdin.write(b"".join(json.dumps(message).encode() + b"\n" for message in messages))
            await process.stdin.drain()
            replies, notices = {}, []
            while not any(notice["kind"] == "settled" for notice in notices):
                frame = await receive()
                if frame.get("method") == "budget/authorize":
                    await send({"jsonrpc": "2.0", "id": frame["id"], "result": {**trace, "allow": True}})
                elif frame.get("method") == "session/update":
                    notices.append(frame["params"])
                else:
                    replies[frame["id"]] = frame
            assert replies["l-3"]["result"]["accepted"]
            assert replies["l-4"]["result"]["status"] == "queued"
            applied, = [notice for notice in notices if notice["kind"].startswith("steer_")]
            assert applied["kind"] == "steer_applied" and applied["message_id"] == "before-start"
            assert applied["after_tool_batch"] is False and applied["new_turn"] is False
            ended = [notice["request_id"] for notice in notices[:notices.index(applied)] if notice["kind"] == "request_end"]
            assert applied["after_request_id"] == (ended[-1] if ended else None)
            assert notices[-1]["outcome"] == "completed"
            assert any(STEER in json.dumps(request.body["input"]) for request in api.requests)
    run(exercise())
