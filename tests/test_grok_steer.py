"""Grok Worker ACP steering through fake processes and the Worker's channel order."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from fake_grok_acp import FAKE_ACP
from looper.agents.room import MemberKind, RoomInput, Seat, room_section
from looper.agents.runtimes import Capability, RoomPush, RuntimeUnsupported, StreamChannel, room_push
from looper.agents.runtimes.channel import DeliveryResult
from looper.agents.runtimes.grok import GrokRuntime
from looper.agents.runtimes.grok_steer import GrokChannel
from looper.agents.tiers.worker import room_input
from test_grok_runtime import _home, _launch, _operator, _prompt_file


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _steering_launch():
    launch = _launch()
    runtime = dataclasses.replace(launch.profile.runtime, capabilities=launch.profile.runtime.capabilities | {Capability.STEER})
    return dataclasses.replace(launch, settings=dataclasses.replace(
        launch.settings, profile=dataclasses.replace(launch.profile, runtime=runtime)))


@pytest.mark.parametrize(("version", "code", "message", "supported", "checked"), [
    ("grok 1.0.76", -32602, "session not found", False, False),
    ("grok 1.0.77", -32602, "session not found", True, True),
    ("grok 1.0.78", -32602, "session not found", True, True),
    ("grok 1.0.77", -32601, "method not found", False, True),
    ("grok 1.0.77", -32603, "internal error", False, True),
    ("grok 1.0.77", -32602, "invalid params", False, True),
])
def test_probe_requires_the_minimum_version_and_the_interject_method(
    tmp_path: Path, version: str, code: int, message: str, supported: bool, checked: bool,
) -> None:
    # Embed the fake settings: the runtime deliberately filters inherited variables.
    body = ("import os\n"
            f"os.environ.update({{'FAKE_ACP_ROOT': {str(tmp_path)!r}, 'FAKE_ACP_VERSION': {version!r}, "
            f"'FAKE_ACP_INTERJECT_CODE': {str(code)!r}, 'FAKE_ACP_INTERJECT_MESSAGE': {message!r}}})\n" + FAKE_ACP)
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
    launch = dataclasses.replace(_steering_launch(), consult=True)
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


@pytest.mark.parametrize("scenario", [
    "STEER_TOOL", "STEER_FINAL", "STEER_LATE", "STEER_REFUSE", "STEER_EMPTY", "STEER_TOOL_ONLY",
])
def test_acceptance_is_not_read_until_model_output_after_a_later_tool_result(tmp_path: Path, scenario: str) -> None:
    async def run() -> None:
        home = tmp_path / "home"
        home.mkdir()
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-c", FAKE_ACP, "agent", "--always-approve", "stdio",
            cwd=tmp_path, env={**os.environ, "HOME": str(home), "GROK_HOME": str(home), "FAKE_ACP_ROOT": str(tmp_path)},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        channel = GrokChannel(process, model="grok-4.7", cwd=str(tmp_path))
        events: list[dict[str, Any]] = []
        tool_finished = asyncio.Event()

        async def consume() -> None:
            async for event in channel.events():
                events.append(event)
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
            result = await asyncio.wait_for(channel.send("new instructions", uuid="delivery-1"), 3)
            assert result is (DeliveryResult.REJECTED if scenario == "STEER_REFUSE" else DeliveryResult.ACCEPTED)
            await _wait_file(tmp_path / "steer-accepted")
            assert not _receipts(events)
            assert not (tmp_path / "steer-tool.done").exists()
            requests = _records(tmp_path / "steer-requests.jsonl")
            assert [r["method"] for r in requests] == ["_x.ai/interject"]
            assert requests[0]["params"] == {
                "sessionId": events[0]["session_id"], "text": "new instructions", "interjectionId": "delivery-1"}
            (tmp_path / "steer-tool.finish").touch()
            if scenario not in ("STEER_FINAL", "STEER_LATE"):
                await asyncio.wait_for(tool_finished.wait(), 2)
                assert not _receipts(events)  # The result alone is not model consumption.
                (tmp_path / "steer-model.start").touch()
            await asyncio.wait_for(task, 3)
            await asyncio.wait_for(process.wait(), 2)
            receipts = _receipts(events)
            assert [e["uuid"] for e in receipts] == (
                ["delivery-1"] if scenario in ("STEER_TOOL", "STEER_TOOL_ONLY") else [])
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
            if process.returncode is None:
                process.kill()
            await process.wait()

    asyncio.run(run())


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
