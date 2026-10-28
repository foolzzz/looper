"""Shared baseline against a runner subprocess and the production bridge/codec."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys

import pytest

from looper.agents.runtimes.api.loop import ToolOutcome
from looper.agents.runtimes.pricing import ModelPrice
from looper.agents.runtimes.runner import protocol as p
from looper.agents.runtimes.runner.bridge import RpcFailure, RunnerBridge
from looper.agents.runtimes.runner.turn import RunnerTurn

ROOT = Path(__file__).resolve().parents[1]
CASES = {case["name"]: case for case in json.loads((ROOT / "runners/protocol/cases.json").read_text())}
RAW = dict(input_uncached=1000, output=200, cache_read=300, cache_write=400)


def run(coroutine):
    asyncio.run(asyncio.wait_for(coroutine, 10))


@asynccontextmanager
async def runner(tmp_path, initialize=True):
    env = dict(os.environ, HOME=str(tmp_path))
    env.pop("PYTHONPATH", None)
    process = await asyncio.create_subprocess_exec(sys.executable, "-u", str(ROOT / "runners/fake/main.py"),
        cwd=tmp_path, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    diagnostics = asyncio.create_task(process.stderr.read())
    bridge = RunnerBridge(process.stdout, process.stdin, "fake", control_timeout=2)
    try:
        if initialize:
            await bridge.initialize(permitted=("turn",))
        yield bridge
    finally:
        await bridge.close()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except TimeoutError:
            process.kill()
            await process.wait()
        evidence = await diagnostics
        bridge.fake_evidence = [json.loads(line) for line in evidence.splitlines() if line.startswith(b"{")]
        assert b"test-secret" not in evidence
        assert process.returncode == 0, evidence.decode()


class Tools:
    def __init__(self, gate=None):
        self.calls, self.entered, self.gate = [], asyncio.Event(), gate

    async def call(self, call):
        self.calls.append(call)
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        failed = json.loads(call.arguments).get("action") == "fake-unsupported"
        return ToolOutcome("business operation failed" if failed else "task-positive-fact", is_error=failed, ran=True)


async def begin(bridge, *, script=None, cap=None, gate=None, role="manager", cancel_on_cap=False, held_authorization=None):
    new = dict(CASES["new"]["message"]["params"], role=role)
    new["env"] = {"LOOPER_PROVIDER_ACCOUNT_ID": "account"}
    sid = await bridge.open_session(new)
    tools = Tools(gate)
    async def limits():
        return dict(run_cap=cap, task_cap=None, task_spent=0)
    async def cancel(_):
        await bridge.cancel_turn(sid, "trace")
    turn = RunnerTurn(session_id=sid, trace_id="trace", role=role, tools=tools,
        provider_id="account", model="m", price=lambda _, usage: ModelPrice(1, 2, 3, 4).cost(usage), limits=limits,
        cancel=cancel if cancel_on_cap else None)
    if held_authorization:
        entered, release = held_authorization
        async def held(params):
            entered.set()
            await release.wait()
            return {"session_id": sid, "trace_id": "trace", "allow": False, "reason": "stopped"}
        turn.authorize = held
    events = []
    tool_name = "looper_tasks" if role == "manager" else "looper_task_profiles"
    await bridge.begin_turn(sid, "trace", events.append, turn,
        text=json.dumps({"fake_script": script}) if script else CASES["prompt-unicode"]["message"]["params"]["text"],
        tools=[dict(name=tool_name, description="read", inputSchema={"type": "object"})])
    return sid, turn, tools, events


def test_initialize_and_version_rejections(tmp_path):
    async def exercise():
        async with runner(tmp_path, initialize=False) as bridge:
            init = CASES["initialize"]["message"]["params"]
            with pytest.raises(RpcFailure) as error:
                await bridge.request("initialize", dict(init, protocol_versions=["9.9"]))
            assert error.value.code == p.ErrorCode.VERSION_MISMATCH
            result = await bridge.initialize(permitted=("turn",))
            assert result["runner"] == {"kind": "fake", "version": "0.1.0"}
            assert result["protocol_version"] == "0.1"
            assert result["capabilities"] == dict(max_sessions=4, per_session_isolation=True,
                per_session_credentials=True, cancel=True, steer=dict(in_trace=True,
                    boundary="after_tool_batch", idle_start=False),
                usage=dict(per_request=True, buckets=True, reasoning_in_output=False), authorize_covers=["turn"])
            with pytest.raises(RpcFailure) as error:
                await bridge.request("initialize", init)
            assert error.value.code == p.ErrorCode.INVALID_REQUEST
    run(exercise())


@pytest.mark.parametrize("role", ["manager", "planner"])
def test_ordinary_turn_uses_platform_fact_and_host_charge(tmp_path, role):
    async def exercise():
        async with runner(tmp_path) as bridge:
            sid, turn, tools, events = await begin(bridge, role=role)
            summary = await bridge.wait_settled(sid, "trace")
            assert summary.outcome == "completed" and summary.usage_complete
            assert len(tools.calls) == 1
            assert tools.calls[0].name == ("looper_tasks" if role == "manager" else "looper_task_profiles")
            assert json.loads(tools.calls[0].arguments) == ({"action": "list"} if role == "manager" else {})
            assert [e["type"] for e in events] == ["system", "stream_event", "assistant", "user", "usage", "result"]
            assert "task-positive-fact" in events[-1]["result"]
            if role == "planner":
                assert json.loads(events[-1]["result"])["action"] == "none"
            assert summary.usage.cost_usd == 0.0039
            assert (summary.usage.input_tokens, summary.usage.output_tokens,
                    summary.usage.cache_read_tokens, summary.usage.cache_creation_tokens) == tuple(RAW.values())
            assert all(e["trace_id"] == "trace" and e["session_id"] == sid for e in events)
            usage = next(e for e in events if e["type"] == "usage")
            assert usage["request_id"] and events[-1]["num_turns"] == 1
        ended = [event for event in bridge.fake_evidence if event["event"] == "request_end"]
        assert len(ended) == 1 and ended[0]["claimed_cost_usd"] == 999
    run(exercise())


def test_denied_authorization_performs_no_model_or_tools(tmp_path):
    async def exercise():
        async with runner(tmp_path) as bridge:
            sid, _, tools, events = await begin(bridge, cap=0.00001, cancel_on_cap=True)
            summary = await bridge.wait_settled(sid, "trace")
            assert summary.outcome == "capped" and summary.cap_reason == "run_cap"
            assert summary.usage.cost_usd == 0 and summary.usage_complete
            assert not tools.calls and [e["type"] for e in events] == ["system", "result"]
            assert events[-1]["num_turns"] == 0
    run(exercise())


@pytest.mark.parametrize("cancel", [False, True], ids=["apply-at-boundary", "cancel-drops-queued"])
def test_in_trace_steer_and_cancel(tmp_path, cancel):
    async def exercise():
        gate = asyncio.Event() if cancel else None
        async with runner(tmp_path) as bridge:
            sid, _, tools, events = await begin(bridge, script="pause-at-boundary", gate=gate)
            try:
                await tools.entered.wait()
                assert (await bridge.steer(sid, "trace", "message", "adjust"))["status"] == "queued"
                if cancel:
                    await bridge.cancel_turn(sid, "trace")
            finally:
                if gate is not None:
                    gate.set()
            summary = await bridge.wait_settled(sid, "trace")
            assert summary.outcome == ("cancelled" if cancel else "completed")
            steer = [e for e in events if e.get("subtype") in {"steer_applied", "steer_dropped"}]
            assert len(steer) == 1
            assert steer[0]["subtype"] == ("steer_dropped" if cancel else "steer_applied")
            assert steer[0]["runner"]["message_id"] == "message"
            if not cancel:
                assert steer[0]["runner"]["after_tool_batch"] and not steer[0]["runner"]["new_turn"]
                assert "Applied steer: adjust" in events[-1]["result"]
            # Closing the session releases it; it cannot cancel another session.
            await bridge.close_session(sid)
            other, _, _, _ = await begin(bridge)
            assert (await bridge.wait_settled(other, "trace")).outcome == "completed"
    run(exercise())


def test_platform_business_failure_cannot_report_success(tmp_path):
    async def exercise():
        async with runner(tmp_path) as bridge:
            sid, _, tools, events = await begin(bridge, script="tool-error")
            summary = await bridge.wait_settled(sid, "trace")
            assert summary.outcome == "failed" and len(tools.calls) == 1
            tool_result = next(e for e in events if e["type"] == "user")["message"]["content"][0]
            assert tool_result["is_error"] and tool_result["content"] == "business operation failed"
            assert events[-1]["is_error"] and summary.usage.cost_usd == 0.0039
    run(exercise())


def test_cancel_settles_while_authorization_reply_is_held(tmp_path):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        async with runner(tmp_path) as bridge:
            try:
                sid, _, tools, events = await begin(bridge, held_authorization=(entered, release))
                await entered.wait()
                await bridge.steer(sid, "trace", "queued", "adjust")
                await bridge.cancel_turn(sid, "trace")
                summary = await asyncio.wait_for(bridge.wait_settled(sid, "trace"), 1)
                assert summary.outcome == "cancelled" and not tools.calls
                dropped = [e for e in events if e.get("subtype") == "steer_dropped"]
                assert len(dropped) == 1 and dropped[0]["runner"]["message_id"] == "queued"
                assert not any(e.get("subtype") == "steer_applied" for e in events)
            finally:
                release.set()
    run(exercise())
