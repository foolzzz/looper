"""Grok Worker ACP steering through fake processes and the Worker's channel order."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fake_claude import fake_claude
from fake_grok_acp import FAKE_ACP
from looper.agents.agent_settings import Access
from looper.agents.bus import MessageBus
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.room import MemberKind, RoomInput, Seat, room_section
from looper.agents.runtimes import Capability, RoomPush, RuntimeUnsupported, StreamChannel, room_push
from looper.agents.runtimes.channel import DeliveryResult
from looper.agents.runtimes.grok import GrokRuntime
from looper.agents.runtimes.grok_steer import GrokChannel
from looper.agents.tiers import worker as worker_module
from looper.agents.tiers.planner import PlanStep, PlannerAgent, TaskPlan
from looper.agents.tiers.planner_protocol import ChatAction, ChatReply
from looper.agents.tiers.worker import ProcessRun, WorkerConfig, WorkerPool, room_input
from test_agent_planner_worker import _worker_row
from test_grok_runtime import _home, _launch, _operator, _prompt_file


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _steering_launch(*, executable: str = "grok"):
    launch = _launch(executable=executable)
    runtime = dataclasses.replace(launch.profile.runtime, capabilities=launch.profile.runtime.capabilities | {Capability.STEER})
    return dataclasses.replace(launch, settings=dataclasses.replace(
        launch.settings, profile=dataclasses.replace(launch.profile, runtime=runtime)))


@pytest.mark.parametrize(("version", "code", "message", "data", "supported", "checked"), [
    ("grok 1.0.76", -32602, "Invalid params", "session not found: probe", False, False),
    ("grok 1.0.77", -32602, "session not found: probe", None, True, True),
    ("grok 1.0.78", -32602, "Invalid params", "session not found: probe", True, True),
    ("grok 1.0.77", -32602, "Invalid params", {"reason": "Session Not Found: probe"}, True, True),
    ("grok 1.0.77", -32602, "Invalid params", [{"reason": "session not found: probe"}], True, True),
    ("grok 1.0.77", -32601, "method not found", "session not found: probe", False, True),
    ("grok 1.0.77", -32603, "internal error", "session not found: probe", False, True),
    ("grok 1.0.77", -32602, "Invalid params", None, False, True),
    ("grok 1.0.77", -32602, "Invalid params", {"reason": "invalid session"}, False, True),
])
def test_probe_requires_the_minimum_version_and_the_interject_method(
    tmp_path: Path, version: str, code: int, message: str, data: Any, supported: bool, checked: bool,
) -> None:
    # Embed the fake settings: the runtime deliberately filters inherited variables.
    body = ("import os\n"
            f"os.environ.update({{'FAKE_ACP_ROOT': {str(tmp_path)!r}, 'FAKE_ACP_VERSION': {version!r}, "
            f"'FAKE_ACP_INTERJECT_CODE': {str(code)!r}, 'FAKE_ACP_INTERJECT_MESSAGE': {message!r}, "
            f"'FAKE_ACP_INTERJECT_DATA': {json.dumps(data)!r}}})\n" + FAKE_ACP)
    executable = fake_claude(tmp_path / "grok", body)
    probe = GrokRuntime().probe(executable)
    assert probe.error is None and probe.version == version
    assert (Capability.STEER in probe.capabilities) is supported
    requests = _records(tmp_path / "acp-requests.jsonl")
    assert bool(requests) is checked
    assert all(request.get("method") != "session/prompt" for request in requests)
    if checked:
        interject, = [r for r in requests if r.get("method") == "_x.ai/interject"]
        assert isinstance(interject["params"]["sessionId"], str)
        assert isinstance(interject["params"]["text"], str)
        assert not any(r.get("method") == "session/new" for r in requests)


def test_steering_command_keeps_the_run_home_and_restricts_tools_in_an_agent_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = GrokRuntime()
    _operator(tmp_path, monkeypatch)
    launch = dataclasses.replace(_steering_launch(), access=Access.CONSULT)
    command = runtime.steer_command("grok", cwd=tmp_path, prompt_file=_prompt_file(tmp_path), launch=launch,
                                    max_turns=3)
    assert command[:3] == ["env", f"GROK_HOME={_home(command)}", f"HOME={_home(command)}"]
    assert "agent" in command and command[-1] == "stdio" and "--always-approve" in command
    assert "--prompt-file" not in command and "--tools" not in command
    profile = Path(command[command.index("--agent-profile") + 1])
    assert profile.is_relative_to(_home(command))
    assert "tools: read_file,grep,list_dir" in profile.read_text()
    assert room_push(launch) is RoomPush.STEER
    for review in (False, True):
        mode = room_input(launch, is_verify=review)
        assert mode is RoomInput.PUSHED
        seat = Seat("reviewer@s1" if review else "worker@s1", "reviewer" if review else "worker",
                    MemberKind.REVIEW if review else MemberKind.WORK, run="w1")
        assert "Do not poll your inbox" in room_section(seat, mode)


def test_without_steering_the_worker_keeps_its_headless_command_and_pull_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = GrokRuntime()
    _operator(tmp_path, monkeypatch)
    launch = _launch()
    command = runtime.run_command("grok", cwd=tmp_path, prompt_file=_prompt_file(tmp_path), launch=launch)
    assert "--prompt-file" in command and "streaming-messages-json" in command and "agent" not in command
    assert isinstance(runtime.run_channel(None, launch=launch, cwd=tmp_path), StreamChannel)  # type: ignore[arg-type]
    assert room_push(launch) is RoomPush.NONE
    assert room_input(launch, is_verify=False) is RoomInput.PULL
    with pytest.raises(RuntimeUnsupported):
        runtime.steer_command("grok", cwd=tmp_path, prompt_file=_prompt_file(tmp_path), launch=launch)


async def _wait_file(path: Path) -> None:
    async with asyncio.timeout(3):
        while not path.exists():
            await asyncio.sleep(0.01)


def _receipts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e.get("type") == "user" and e.get("isReplay") is True]


@pytest.mark.parametrize(("scenario", "delivery_id", "flat"), [
    *[(scenario, delivery_id, False)
      for scenario in ("STEER_TOOL", "STEER_FINAL", "STEER_LATE", "STEER_REFUSE", "STEER_EMPTY", "STEER_TOOL_ONLY")
      for delivery_id in ("planner-input-1", "looper-reminder-0123456789ab-42")
      if delivery_id == "planner-input-1" or scenario not in ("STEER_FINAL", "STEER_LATE")],
    ("STEER_TOOL", "planner-input-1", True),
])
def test_acceptance_is_not_read_until_model_output_after_a_later_tool_result(
    tmp_path: Path, scenario: str, delivery_id: str, flat: bool,
) -> None:
    async def run() -> None:
        home = tmp_path / "home"
        home.mkdir()
        handle = ProcessRun(GrokRuntime(), [
            "env", f"HOME={home}", f"GROK_HOME={home}", f"FAKE_ACP_ROOT={tmp_path}",
            f"FAKE_ACP_INTERJECT_FLAT={int(flat)}",
            sys.executable, "-u", "-c", FAKE_ACP, "agent", "--always-approve", "stdio",
        ], launch=_steering_launch(), cwd=tmp_path, session_dir=None, label="test-grok")
        assert handle.dropped_input() == []
        await handle.launch()
        process, channel = handle.process, handle.channel
        assert process is not None and isinstance(channel, GrokChannel)
        events: list[dict[str, Any]] = []
        tool_started = asyncio.Event()
        tool_finished = asyncio.Event()

        async def consume() -> None:
            async for event in channel.events():
                events.append(event)
                if any(b.get("type") == "tool_use" and b.get("id") == "steer-tool"
                       for b in (event.get("message") or {}).get("content", []) if isinstance(b, dict)):
                    tool_started.set()
                if any(b.get("type") == "tool_result" for b in (event.get("message") or {}).get("content", [])
                       if isinstance(b, dict)):
                    tool_finished.set()
                if event.get("type") == "result":
                    channel.end_input()

        task = None
        try:
            # Match Worker ordering: start() must return without a stdout consumer.
            await asyncio.wait_for(channel.start(scenario), 2)
            task = asyncio.create_task(consume())
            await _wait_file(tmp_path / "steer-ready")
            if scenario not in ("STEER_FINAL", "STEER_LATE"):
                # The child being ready does not mean the channel has read its tool call.
                await asyncio.wait_for(tool_started.wait(), 2)
            result = await asyncio.wait_for(channel.send("new instructions", uuid=delivery_id), 3)
            assert result is (DeliveryResult.REJECTED if scenario == "STEER_REFUSE" else DeliveryResult.ACCEPTED)
            await _wait_file(tmp_path / "steer-accepted")
            assert not _receipts(events)
            assert not (tmp_path / "steer-tool.done").exists()
            requests = _records(tmp_path / "steer-requests.jsonl")
            assert [r["method"] for r in requests] == ["_x.ai/interject"]
            assert requests[0]["params"] == {
                "sessionId": events[0]["session_id"], "text": "new instructions", "interjectionId": delivery_id}
            (tmp_path / "steer-tool.finish").touch()
            if scenario not in ("STEER_FINAL", "STEER_LATE"):
                await asyncio.wait_for(tool_finished.wait(), 2)
                assert not _receipts(events)  # The result alone is not model consumption.
                (tmp_path / "steer-model.start").touch()
            await asyncio.wait_for(task, 3)
            await asyncio.wait_for(process.wait(), 2)
            receipts = _receipts(events)
            assert [e["uuid"] for e in receipts] == (
                [delivery_id] if scenario in ("STEER_TOOL", "STEER_TOOL_ONLY") else [])
            missed = (["new instructions"] if delivery_id == "planner-input-1"
                      and scenario in ("STEER_FINAL", "STEER_LATE", "STEER_EMPTY") else [])
            assert handle.dropped_input() == missed
            assert channel.dropped_input() == missed
            channel.end_input()
            assert handle.dropped_input() == missed  # Closing input must preserve recovery data.
            if scenario == "STEER_TOOL_ONLY":
                next_call = next(e for e in events if any(
                    b.get("type") == "tool_use" and b.get("id") == "next-tool"
                    for b in (e.get("message") or {}).get("content", []) if isinstance(b, dict)))
                assert events.index(receipts[0]) < events.index(next_call)
            result_event, = [e for e in events if e["type"] == "result"]
            assert not result_event["is_error"]
            assert result_event["total_cost_usd"] == pytest.approx(0.005)
            assert all(r["method"] != "session/cancel" for r in _records(tmp_path / "steer-requests.jsonl"))
            assert process.stderr is not None
            assert await process.stderr.read() == b""
        finally:
            channel.end_input()
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await handle.end()

    asyncio.run(run())


def test_planner_queued_input_during_the_final_response_reaches_the_work_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_db: Any,
) -> None:
    _operator(tmp_path, monkeypatch)
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 10, "max_turns": 0})
    report = json.dumps({"status": "completed", "summary": "done", "changed_files": {}})
    settings = {"FAKE_ACP_ROOT": str(tmp_path), "FAKE_ACP_STEER_SCENARIO": "STEER_FINAL",
                "FAKE_ACP_STEER_RESULT": report}
    executable = fake_claude(tmp_path / "grok", f"import os\nos.environ.update({settings!r})\n" + FAKE_ACP)
    probe = GrokRuntime().probe(executable)
    assert probe.error is None and Capability.STEER in probe.capabilities
    launch = _launch(executable=executable)
    runtime = dataclasses.replace(launch.profile.runtime, capabilities=probe.capabilities)
    launch = dataclasses.replace(launch, settings=dataclasses.replace(
        launch.settings, profile=dataclasses.replace(launch.profile, runtime=runtime)))
    monkeypatch.setattr(worker_module, "load_agent_settings", AsyncMock(return_value=launch.settings))

    async def run() -> None:
        bus = MessageBus(runtime_root=tmp_path / "runtime")
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path / "runtime")
        planner = PlannerAgent(task_id="t1", runtime_root=tmp_path / "runtime")
        step = PlanStep(step_id="s1", index=0, instruction="write it", status="dispatched", worker_id="w1")
        plan = TaskPlan(plan_id="p1", goal_id="", requirement="build it", work_dir=str(tmp_path), steps=[step])
        planner._bus, planner._plan = bus, plan
        results: asyncio.Queue[MessageEnvelope] = asyncio.Queue()
        acknowledgements: list[MessageEnvelope] = []
        bus.subscribe_topic(MessageType.WORK_RESULT, results.put_nowait)
        bus.subscribe_topic(MessageType.WORKER_INPUT_RESULT, acknowledgements.append)
        await pool.start(bus)
        process = None
        try:
            await planner._save(plan, step)
            await pool._dispatch(MessageEnvelope.create(
                source=planner_address("t1"), target=worker_address("w1"), message_type=MessageType.WORK_ITEM,
                payload={"task_id": "s1", "instruction": "write it", "context": {"work_dir": str(tmp_path)}},
            ))
            await _wait_file(tmp_path / "steer-ready")
            process = pool._active["w1"].process
            # The response is already open: completing it cannot consume the queued steer.
            answer = await planner._apply_chat(plan, ChatReply(
                "Passing it on.", ChatAction.FORWARD, step.label, "use blue", ()), None)
            assert answer == "Passing it on."
            assert [ack.payload for ack in acknowledgements] == [{"delivered": True}]
            assert step.missed_messages == []
            assert agent_db.store.steps["t1"]["s1"]["missed_messages"] == []
            (tmp_path / "steer-tool.finish").touch()
            result = await asyncio.wait_for(results.get(), 3)
            assert result.payload["status"] == "completed", result.payload
            interject, = _records(tmp_path / "steer-requests.jsonl")
            assert interject["params"]["text"].endswith("use blue")
            assert result.payload["missed_input"] == [interject["params"]["text"]]
            assert process is not None
            await asyncio.wait_for(process.wait(), 2)
        finally:
            await pool.stop()
            await bus.shutdown()
            if process is not None:
                assert process.returncode is not None

    asyncio.run(asyncio.wait_for(run(), 10))


def test_a_worker_turn_emulates_the_model_response_limit(tmp_path: Path) -> None:
    async def run() -> None:
        home = tmp_path / "home"
        home.mkdir()
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-c", FAKE_ACP, "agent", "--always-approve", "stdio",
            cwd=tmp_path, env={**os.environ, "HOME": str(home), "GROK_HOME": str(home), "FAKE_ACP_ROOT": str(tmp_path)},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        channel = GrokChannel(process, model="grok-4.7", cwd=str(tmp_path), max_turns=2)
        events = []
        try:
            await asyncio.wait_for(channel.start("LOOP"), 2)
            async with asyncio.timeout(6):
                async for event in channel.events():
                    events.append(event)
                    if event.get("type") == "result":
                        channel.end_input()
            await asyncio.wait_for(process.wait(), 2)
            result, = [e for e in events if e["type"] == "result"]
            assert result["is_error"] and result["subtype"] == "error_max_turns"
            assert (tmp_path / "cancels.log").read_text() == "cancel\n"
        finally:
            channel.end_input()
            if process.returncode is None:
                process.kill()
            await process.wait()

    asyncio.run(run())
