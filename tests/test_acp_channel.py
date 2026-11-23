"""ACP v1 Reviewer turns use the real stdio channel, with no CLI or model."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from looper.agents import proc_tree
from looper.agents.claude_cli import ClaudeRunRecorder
from looper.agents.run_usage import reported_totals
from looper.agents.runtimes.acp import AcpRuntime
from looper.agents.runtimes.acp_channel import AcpChannel
from looper.agents.tiers.worker import RunOutcome, judge_result, run_outcome


FAKE = Path(__file__).with_name("fake_acp.py")


async def _turn(tmp_path, scenario="success", *, unreadable=False):
    contract = tmp_path / "contract.md"
    if not unreadable:
        contract.write_bytes(b"system contract\r\nverbatim\n")
    trace = tmp_path / "requests.jsonl"
    process = await proc_tree.spawn(sys.executable, str(FAKE), "--scenario", scenario, "--trace", str(trace),
        cwd=tmp_path, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    channel = AcpChannel(process, cwd=str(tmp_path), prompt_file=contract, model="declared-model")
    try:
        await channel.start("verify exactly this")
        async with asyncio.timeout(3):
            events = [event async for event in channel.events()]
        return events, [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
    finally:
        await proc_tree.end(process, label="fake ACP review")


def test_one_session_one_prompt_with_verbatim_contract_first(tmp_path):
    async def run():
        events, requests = await _turn(tmp_path)
        assert events[0]["type"] == "system" and events[0]["subtype"] == "init"
        assert events[0]["runtime"] == "acp" and events[0]["session_id"] == "review-session"
        assert [request["method"] for request in requests] == ["initialize", "session/new", "session/prompt"]
        assert requests[0]["params"]["protocolVersion"] == 1
        assert requests[1]["params"] == {"cwd": str(tmp_path), "mcpServers": []}
        assert requests[2]["params"]["prompt"] == [
            {"type": "text", "text": "system contract\r\nverbatim\n"}, {"type": "text", "text": "verify exactly this"}]
        result = events[-1]
        verdict = judge_result(result, work_dir=tmp_path, is_verify=True)
        assert verdict["passed"] is True
        assert run_outcome(result, verdict, is_verify=True) is RunOutcome.PASSED
    asyncio.run(run())


@pytest.mark.parametrize(("scenario", "expected"), [
    ("v2", RunOutcome.RUN_FAILED), ("v2_prompt", RunOutcome.RUN_FAILED),
    ("early_exit", RunOutcome.RUN_FAILED), ("no_text", RunOutcome.RUN_FAILED),
    ("no_verdict", RunOutcome.CONTRACT), ("not_passed", RunOutcome.NOT_PASSED),
])
def test_failure_mapping_preserves_the_existing_reviewer_contract(tmp_path, scenario, expected):
    async def run():
        events, _ = await _turn(tmp_path, scenario)
        result = events[-1]
        verdict = judge_result(result, work_dir=tmp_path, is_verify=True)
        assert run_outcome(result, verdict, is_verify=True) is expected
    asyncio.run(run())


def test_session_new_timeout_is_a_failed_run(tmp_path, monkeypatch):
    async def run():
        from looper.agents.runtimes import acp_channel
        monkeypatch.setattr(acp_channel, "SESSION_TIMEOUT_SEC", 0.05)
        events, requests = await _turn(tmp_path, "session_timeout")
        assert events[-1]["is_error"] is True
        assert "session/new" in events[-1]["errors"][0]
        assert not any(request["method"] == "session/prompt" for request in requests)
    asyncio.run(run())


def test_unreadable_prompt_file_never_sends_a_prompt(tmp_path):
    async def run():
        events, requests = await _turn(tmp_path, unreadable=True)
        assert events[-1]["is_error"] is True
        assert "prompt_file" in events[-1]["errors"][0]
        assert not any(request["method"] == "session/prompt" for request in requests)
    asyncio.run(run())


@pytest.mark.parametrize(("scenario", "selected"), [
    ("permission_write", "no"), ("permission_unknown", "no"), ("permission_misleading", "no"),
    ("permission_read", "yes"), ("permission_search", "yes"),
])
def test_only_structured_read_only_tool_kinds_can_be_allowed(tmp_path, scenario, selected):
    async def run():
        events, requests = await _turn(tmp_path, scenario)
        reply, = [request for request in requests if request.get("id") == "permission-1"]
        assert reply["result"]["outcome"] == {"outcome": "selected", "optionId": selected}
        assert events[-1]["subtype"] == "success"
        assert bool(AcpRuntime().permission_denials(events[-1])) is (selected == "no")
    asyncio.run(run())


@pytest.mark.parametrize("method", [
    "fs/write_text_file", "terminal/create", "terminal/output", "terminal/wait_for_exit",
    "terminal/kill", "terminal/release",
])
def test_filesystem_writes_and_terminal_requests_are_denied(tmp_path, method):
    async def run():
        contract = tmp_path / "contract.md"
        contract.write_text("contract")
        target = tmp_path / "protected.txt"
        target.write_text("unchanged")
        reply_file = tmp_path / "reply.json"
        program = r'''
import json, sys
from pathlib import Path
def receive(): return json.loads(sys.stdin.readline())
def send(**value): print(json.dumps({"jsonrpc": "2.0", **value}), flush=True)
send(id=receive()["id"], result={"protocolVersion": 1})
send(id=receive()["id"], result={"sessionId": "review-session"})
prompt = receive()
send(id="client-write", method=sys.argv[1], params={"sessionId": "review-session",
     "path": sys.argv[2], "content": "overwritten", "command": "touch", "args": [sys.argv[2]],
     "terminalId": "uncreated-terminal"})
Path(sys.argv[3]).write_text(json.dumps(receive()))
send(method="session/update", params={"sessionId": "review-session", "update": {
     "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "review complete"}}})
send(id=prompt["id"], result={"stopReason": "end_turn"})
'''
        process = await proc_tree.spawn(sys.executable, "-c", program, method, str(target), str(reply_file),
            cwd=tmp_path, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        channel = AcpChannel(process, cwd=str(tmp_path), prompt_file=contract, model="declared-model")
        try:
            await channel.start("verify")
            async with asyncio.timeout(3):
                events = [event async for event in channel.events()]
            reply = json.loads(reply_file.read_text())
            assert reply == {"jsonrpc": "2.0", "id": "client-write", "error": {
                "code": -32000, "message": "ACP Reviewer client request denied"}}
            assert events[-1]["permission_denials"] == [{"tool": method, "input": "unsupported client request"}]
            assert target.read_text() == "unchanged"
        finally:
            await proc_tree.end(process, label="denied fake ACP client request")
    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["success", "vendor_usage"])
def test_unreported_usage_and_vendor_extensions_never_become_zero_cost(tmp_path, scenario):
    async def run():
        events, _ = await _turn(tmp_path, scenario)
        result = events[-1]
        assert result["usage_complete"] is False
        assert not result.get("usage") and not result.get("modelUsage")
        usage = reported_totals(result, price=lambda model, tokens: 0)
        assert usage.cost_usd is None and usage.input_tokens is None and usage.output_tokens is None
        assert AcpRuntime().cost(usage, None).usage.cost_usd is None
    asyncio.run(run())


def test_documented_dollars_are_notes_and_context_is_not_billable_usage(tmp_path):
    async def run():
        events, _ = await _turn(tmp_path, "usd_cost")
        result = events[-1]
        assert AcpRuntime().result_notes(result)["runtime_reported_usd"] == 0.123
        assert "total_cost_usd" not in result
        assert reported_totals(result, price=lambda model, tokens: 100).cost_usd is None
    asyncio.run(run())


def test_tool_events_use_the_existing_stream_json_shape(tmp_path):
    async def run():
        events, _ = await _turn(tmp_path, "tools")
        tool, = [block for event in events if event["type"] == "assistant"
                 for block in event["message"]["content"] if block["type"] == "tool_use"]
        result, = [block for event in events if event["type"] == "user"
                   for block in event["message"]["content"]]
        assert tool["id"] == result["tool_use_id"] == "read-1"
        assert result["content"] == "example contents"
    asyncio.run(run())


def test_recorder_keeps_missing_usage_as_unknown(tmp_path, monkeypatch):
    async def run():
        events, _ = await _turn(tmp_path, "success")
        recorder = ClaudeRunRecorder(agent_id="reviewer", task_id=None, command=["fake-acp", "stdio"],
                                     cwd=str(tmp_path), input_text="verify")
        monkeypatch.setattr(recorder, "_runtime", lambda: AcpRuntime())
        captured = {}
        async def close(status, **values):
            captured.update(values)
        monkeypatch.setattr(recorder, "_close", close)
        await recorder.finish(output_text=events[-1]["result"], result_event=events[-1], outcome="passed")
        assert captured["usage"].cost_usd is None
        assert captured["metadata"]["usage_complete"] is False
    asyncio.run(run())


def test_cancel_is_sent_before_stdin_closes_without_a_separate_grace_timer(tmp_path):
    async def run():
        contract = tmp_path / "contract.md"
        contract.write_text("contract")
        trace = tmp_path / "requests.jsonl"
        process = await proc_tree.spawn(sys.executable, str(FAKE), "--scenario", "hang", "--trace", str(trace),
            cwd=tmp_path, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        channel = AcpChannel(process, cwd=str(tmp_path), prompt_file=contract, model="declared-model")
        await channel.start("verify")
        ready = asyncio.Event()
        async def consume():
            async for event in channel.events():
                if event["type"] == "assistant":
                    ready.set()
        consumer = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(ready.wait(), 2)
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            # EOF after the queued cancel gives the stand-in a deterministic readback.
            await asyncio.wait_for(process.wait(), 1)
            requests = [json.loads(line) for line in trace.read_text().splitlines()]
            assert requests[-1] == {"jsonrpc": "2.0", "method": "session/cancel",
                                    "params": {"sessionId": "review-session"}}
        finally:
            await proc_tree.end(process, label="cancelled fake ACP review")
    asyncio.run(run())
