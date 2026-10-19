"""Grok's resident session over ACP (``looper/agents/runtimes/grok_acp.py``).

The command and the event translation are checked as pure functions, with notifications shaped as Grok 1.0.62 wrote
them (recorded in the Grok runtime RFC section 14); the session against a stand-in ``grok agent stdio``
(``fake_grok_acp``) that keeps sessions in its home across processes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from fake_grok_acp import FAKE_ACP
from looper.agents.agent_settings import AgentType
from looper.agents.claude_cli import CapStop
from looper.agents.run_usage import Usage
from looper.agents.runtimes import ResidentTransport, adapter
from looper.agents.runtimes import grok_acp
from looper.agents.runtimes.grok import GrokRuntime
from looper.agents.runtimes.grok_acp import (
    AcpGrok, AcpLaunch, AcpTurn, AcpUnavailable, acp_argv, agent_profile, parse_command,
)
from test_grok_runtime import _FAKE_GROK, _launch, _operator, _prompt_file

GROK: GrokRuntime = adapter("grok")  # type: ignore[assignment]


def _command(root: Path, executable: str, prompt: str = "prompt A", *, read_only: bool = False,
             max_turns: int = 0) -> list[str]:
    launch = _launch(executable=executable)
    if read_only:
        launch = dataclasses.replace(launch, role=AgentType.PLANNER)
    return GROK.resident_command(executable, cwd=root, prompt_file=_prompt_file(root, prompt), launch=launch,
                                 max_turns=max_turns)


def _acp_runs(root: Path) -> list[dict[str, Any]]:
    path = root / ".local" / "grok" / "acp-argv.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Events:
    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        self.seen.append(event)

    def kinds(self) -> list[str]:
        return [e["type"] + (":" + e["subtype"] if e.get("subtype") else "") for e in self.seen]


# --- The command ---


def test_the_acp_command_is_the_resident_command_in_acp_flags(tmp_path: Path) -> None:
    command = _command(tmp_path, "grok")
    launch = parse_command(command)
    assert launch == AcpLaunch("grok", "grok-4.7", "low", None, None, 0)
    home = Path(command[1].removeprefix("GROK_HOME="))
    assert acp_argv(launch, home, None) == [
        "env", f"GROK_HOME={home}", f"HOME={home}", "grok", "--trust", "--no-auto-update", "agent",
        "--always-approve", "-m", "grok-4.7", "--effort", "low", "stdio"]
    # Neither the prompt file nor the headless output format reaches the ACP command.
    assert "--prompt-file" not in acp_argv(launch, home, None) and "stream" not in " ".join(acp_argv(launch, home, None))


def test_a_read_only_and_a_capped_command_carry_their_limits_to_the_acp_launch(tmp_path: Path) -> None:
    launch = parse_command(_command(tmp_path, "grok", read_only=True, max_turns=7))
    assert (launch.tools, launch.max_turns) == ("read_file,grep,list_dir", 7)
    assert launch.agent is None
    profile = Path("/home/acp-agent.md")
    argv = acp_argv(launch, Path("/home"), profile)
    assert argv[argv.index("--agent-profile") + 1] == str(profile) and "--tools" not in argv


@pytest.mark.parametrize("flag", [["--sandbox", "off"], ["--mystery"]])
def test_a_flag_with_no_acp_form_leaves_the_session_to_the_per_turn_path(tmp_path: Path, flag: list[str]) -> None:
    with pytest.raises(AcpUnavailable, match=flag[0]):
        parse_command([*_command(tmp_path, "grok"), *flag])


def test_an_agent_profile_restricts_the_tools_and_keeps_the_rest() -> None:
    assert agent_profile(None, "read_file,grep") == "---\nname: looper\ndescription: Looper agent\ntools: read_file,grep\n---\n"
    replacing = "---\nname: looper\ndescription: Looper agent\nprompt_mode: full\n---\n\nRULES\n"
    assert agent_profile(replacing, "read_file") == (
        "---\nname: looper\ndescription: Looper agent\nprompt_mode: full\ntools: read_file\n---\n\nRULES\n")
    with pytest.raises(AcpUnavailable):
        agent_profile("no front matter", "read_file")


# --- A turn's events ---

SESSION = "sess-1"


def _note(kind: str, extension: bool = False, **fields: Any) -> dict[str, Any]:
    return {"method": "_x.ai/session_notification" if extension else "session/update",
            "params": {"sessionId": SESSION, "update": {"sessionUpdate": kind, **fields}}}


def test_a_turn_is_translated_into_claude_code_events_with_the_usage_of_each_response() -> None:
    turn = AcpTurn(model="grok-4.7", session_id=SESSION)
    notes = [
        _note("agent_thought_chunk", content={"type": "text", "text": "hm"}),
        _note("response_completed", True, usage={"input_tokens": 13373, "output_tokens": 46,
                                                 "cache_read_input_tokens": 1152, "cache_creation_input_tokens": 0}),
        _note("tool_call", toolCallId="c1", title="run_terminal_command", rawInput={"command": "echo ACPTOOL"},
              _meta={"x.ai/tool": {"name": "run_terminal_command"}}),
        _note("tool_call_update", toolCallId="c1", status="in_progress"),
        _note("tool_call_update", toolCallId="c1", status="completed",
              rawOutput={"type": "Bash", "output": [65, 67, 75], "exit_code": 0}),
        _note("agent_message_chunk", content={"type": "text", "text": "ACK"}),
        _note("response_completed", True, usage={"input_tokens": 133, "output_tokens": 28,
                                                 "cache_read_input_tokens": 17792, "cache_creation_input_tokens": 0}),
        {"method": "_x.ai/queue/changed", "params": {"sessionId": SESSION}},
    ]
    events = [event for note in notes for event in turn.feed(note)]
    events += turn.finish({"result": {"stopReason": "end_turn", "_meta": {"usage": {
        "inputTokens": 35761, "outputTokens": 101, "cachedReadTokens": 19456, "cacheCreationTokens": 0,
        "modelCalls": 2, "numTurns": 2, "apiDurationMs": 4399, "costUsdTicks": 429440000,
        "modelUsage": {"grok-4.7-build": {"inputTokens": 35761, "outputTokens": 101, "cachedReadTokens": 19456}}}}}},
        duration_ms=4500)
    assert [e["type"] for e in events] == ["assistant", "usage", "user", "assistant", "usage", "result"]
    first, _, tool_result, last, _, result = events
    assert [b["type"] for b in first["message"]["content"]] == ["thinking", "tool_use"]
    assert first["message"]["content"][1]["name"] == "run_terminal_command"
    assert first["message"]["usage"] == {"input_tokens": 13373, "output_tokens": 46, "cache_read_input_tokens": 1152,
                                         "cache_creation_input_tokens": 0}
    # The command's output, a list of byte values for Grok, is readable.
    assert json.loads(tool_result["message"]["content"][0]["content"])["output"] == "ACK"
    assert tool_result["message"]["content"][0]["tool_use_id"] == "c1"
    assert last["message"]["content"] == [{"type": "text", "text": "ACK"}]
    assert all(e["session_id"] == SESSION for e in events if e["type"] != "usage")
    # ACP's turn usage counts the cache hits in its input; the result leaves them out, as headless does.
    assert result["result"] == "ACK" and result["subtype"] == "success" and not result["is_error"]
    assert result["usage"] == {"input_tokens": 16305, "output_tokens": 101, "cache_read_input_tokens": 19456,
                               "cache_creation_input_tokens": 0}
    assert result["modelUsage"]["grok-4.7-build"]["inputTokens"] == 16305
    assert result["total_cost_usd"] == pytest.approx(0.042944)


def test_a_cost_grok_does_not_know_is_left_out_and_a_stop_reason_that_is_not_an_end_is_an_error() -> None:
    turn = AcpTurn(model="grok-4.7", session_id=SESSION)
    (result,) = turn.finish({"result": {"stopReason": "cancelled", "_meta": {"usage": {
        "inputTokens": 10, "outputTokens": 2, "costUsdTicks": 0}}}}, duration_ms=1)
    assert result["is_error"] and result["subtype"] == "error_during_execution"
    assert result["errors"] == ["the turn was cancelled"] and "total_cost_usd" not in result
    (failed,) = AcpTurn(model=None, session_id=SESSION).finish(
        {"error": {"code": -32603, "message": "model unavailable"}}, duration_ms=1)
    assert failed["is_error"] and failed["errors"] == ["model unavailable"]
    assert list(failed["modelUsage"]) == ["grok"]
    (capped,) = AcpTurn(model="m", session_id=SESSION).finish(
        {"result": {"stopReason": "cancelled"}}, duration_ms=1, stop="max_turns")
    assert capped["subtype"] == "error_max_turns"


# --- The session ---


def _go(coro: Any) -> Any:
    """Run a test's coroutine, failing it rather than hanging when a turn never ends."""
    async def bounded() -> Any:
        return await asyncio.wait_for(coro, 40)

    return asyncio.run(bounded())


def _fake(tmp_path: Path) -> str:
    return fake_claude(tmp_path / "grok", FAKE_ACP)


def _fake_with_headless(tmp_path: Path) -> str:
    """A ``grok`` that serves ``agent stdio`` as the ACP stand-in does and anything else as the per-turn one."""
    body = f"import sys\nsrc = {FAKE_ACP!r} if 'agent' in sys.argv[1:] else {_FAKE_GROK!r}\nexec(compile(src, 'fake', 'exec'))\n"
    return fake_claude(tmp_path / "grok", body)


async def _turns(session: AcpGrok, *messages: str, events: Events | None = None) -> list[Any]:
    events = events or Events()
    return [await session.run_turn(message, events) for message in messages]


def test_one_process_serves_every_turn_and_a_restart_loads_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    login = _operator(tmp_path, monkeypatch)
    monkeypatch.setenv("XAI_API_KEY", "xai-operator")
    root = tmp_path / "runtime"
    grok = _fake(tmp_path)
    owner_home = (root / ".local" / "grok" / "sessions" / "planner-task-1").absolute()

    async def _run() -> dict[str, Any]:
        events = Events()
        session = GROK.resident_session("planner-task-1")
        assert isinstance(session, AcpGrok)
        await session.start(_command(root, grok), cwd=str(tmp_path), signature="a", env=_launch().environment())
        assert session.started and session.pid is None  # nothing runs before the first turn
        one, two = await _turns(session, "hello", "again", events=events)
        pid = session.pid
        saved, session_id = session.totals, events.seen[0]["session_id"]
        # The prompt changes: the owner starts over, loading the saved session with its totals.
        await session.stop()
        assert not session.started and session.pid is None
        command = _command(root, grok, "prompt B") + GROK.resume_args(session_id)
        await session.start(command, cwd=str(tmp_path), signature="b", env=_launch().environment(), totals=saved)
        (three,) = await _turns(session, "third", events=events)
        totals = session.totals
        await session.stop()
        return {"turns": [one, two, three], "events": events, "pid": pid, "totals": totals, "id": session_id}

    chat = _go(_run())
    assert [t.result_event["result"] for t in chat["turns"]] == [
        "prompt A | hello | turn 1", "prompt A | again | turn 2", "prompt B | third | turn 3"]
    assert all(t.session_started and t.exit_code is None for t in chat["turns"])
    runs = _acp_runs(root)
    assert len(runs) == 2  # a process for the first two turns, a new one after the restart
    assert {(r["home"], r["user_home"]) for r in runs} == {(str(owner_home), str(owner_home))}
    assert runs[0]["args"][:4] == ["--trust", "--no-auto-update", "agent", "--always-approve"]
    assert all(r["key"] is None for r in runs)  # no API key to fall back to
    assert os.readlink(owner_home / "auth.json") == str(login)
    assert (owner_home / "rules" / "looper.md").read_text().endswith("prompt B")
    # Every event names the session; each turn starts with its init; the tokens are the turn's own.
    assert {e.get("session_id") for e in chat["events"].seen if e["type"] != "usage"} == {chat["id"]}
    assert chat["events"].kinds().count("system:init") == 3
    assert [(t.usage.input_tokens, t.usage.output_tokens, t.usage.cache_read_tokens) for t in chat["turns"]] == \
        [(60, 5, 40)] * 3
    assert (chat["totals"].input_tokens, chat["totals"].output_tokens) == (180, 15)
    assert [t.result_event["total_cost_usd"] for t in chat["turns"]] == [pytest.approx(0.01)] * 3


def test_a_tool_call_is_recorded_with_its_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _operator(tmp_path, monkeypatch)
    root, grok = tmp_path / "runtime", _fake(tmp_path)

    async def _run() -> tuple[Any, Events]:
        events = Events()
        session = AcpGrok(label="manager")
        await session.start(_command(root, grok), cwd=str(tmp_path), signature="s")
        (turn,) = await _turns(session, "TOOL please", events=events)
        await session.stop()
        return turn, events

    turn, events = _go(_run())
    assert events.kinds() == ["system:init", "assistant", "usage", "user", "assistant", "usage", "result:success"]
    assert json.loads(events.seen[3]["message"]["content"][0]["content"])["output"] == "hi\n"
    assert turn.result_event["result"] == "it said hi" and turn.result_event["num_turns"] == 2
    assert turn.result_event["usage"]["input_tokens"] == 160  # 340 in all, 180 of them cache hits


def test_a_turn_given_up_is_cancelled_in_grok_and_the_session_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _operator(tmp_path, monkeypatch)
    root, grok = tmp_path / "runtime", _fake(tmp_path)

    async def _run() -> tuple[Any, list[int | None]]:
        session = AcpGrok(label="manager")
        await session.start(_command(root, grok), cwd=str(tmp_path), signature="s")
        waiting = asyncio.Event()

        async def on_event(event: dict[str, Any]) -> None:
            if event["type"] == "system":
                waiting.set()

        task = asyncio.create_task(session.run_turn("HANG", on_event))
        await waiting.wait()
        before = session.pid
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        (after,) = await _turns(session, "next")
        pids = [before, session.pid]
        stale = session.stale("s")
        await session.stop()
        assert not stale
        return after, pids

    after, pids = _go(_run())
    assert after.result_event["result"].endswith("| next | turn 2")
    assert pids[0] == pids[1] and len(_acp_runs(root)) == 1  # the same process: the cancel did not kill it


def test_a_turn_over_its_soft_cap_ends_with_the_caps_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _operator(tmp_path, monkeypatch)
    root, grok = tmp_path / "runtime", _fake(tmp_path)
    cap = CapStop({"type": "result", "subtype": "error_max_budget_usd", "is_error": True}, Usage(input_tokens=7))

    async def _run() -> tuple[Any, Events, Usage]:
        events = Events()
        session = AcpGrok(label="manager")
        await session.start(_command(root, grok), cwd=str(tmp_path), signature="s")
        # The cap says the turn is over once its first response is in: the turn is cancelled there.
        turn = await session.run_turn("TOOL", events, stop=lambda: cap if len(events.seen) >= 2 else None)
        totals = session.totals
        (next_turn,) = await _turns(session, "next")
        await session.stop()
        assert next_turn.result_event["result"].endswith("turn 2")
        return turn, events, totals

    turn, events, totals = _go(_run())
    assert turn.result_event == cap.result_event and turn.usage == cap.usage
    assert totals == cap.usage
    assert not any(kind.startswith("result") for kind in events.kinds())  # the cancelled turn's own result is not reported


def test_a_turn_over_max_turns_is_cancelled_with_a_max_turns_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _operator(tmp_path, monkeypatch)
    root, grok = tmp_path / "runtime", _fake(tmp_path)

    async def _run() -> Any:
        session = AcpGrok(label="planner-task-1")
        await session.start(_command(root, grok, read_only=True, max_turns=2), cwd=str(tmp_path), signature="s")
        (turn,) = await _turns(session, "LOOP")
        await session.stop()
        return turn

    turn = _go(_run())
    assert turn.result_event["subtype"] == "error_max_turns" and turn.result_event["is_error"]
    # Grok got the cancel and answered it (it logs a cancel it received, then ends the prompt as cancelled).
    assert (root / ".local" / "grok" / "cancels.log").read_text() == "cancel\n"
    assert turn.result_event["stop_reason"] == "max_turns"


def test_a_cancel_that_cannot_be_written_is_bounded_and_ends_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _operator(tmp_path, monkeypatch)
    monkeypatch.setattr(grok_acp, "CANCEL_GRACE_SEC", 0.5)
    root, grok = tmp_path / "runtime", _fake(tmp_path)
    send = AcpGrok._send

    async def blocked(self: AcpGrok, message: dict[str, Any]) -> None:
        if message.get("method") == "session/cancel":
            await asyncio.sleep(3600)  # a writer that never drains
        await send(self, message)

    monkeypatch.setattr(AcpGrok, "_send", blocked)

    async def _run() -> tuple[bool, int | None]:
        session = AcpGrok(label="manager")
        await session.start(_command(root, grok), cwd=str(tmp_path), signature="s")
        waiting = asyncio.Event()

        async def on_event(event: dict[str, Any]) -> None:
            waiting.set()

        task = asyncio.create_task(session.run_turn("HANG", on_event))
        await waiting.wait()
        pid = session.pid
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        stale = session.stale("s")
        await session.stop()
        return stale, pid

    stale, pid = _go(_run())
    assert stale and pid is not None


def test_a_dead_grok_ends_the_turn_though_a_tool_it_left_holds_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _operator(tmp_path, monkeypatch)
    root, grok = tmp_path / "runtime", _fake(tmp_path)

    async def _run() -> Any:
        session = AcpGrok(label="manager")
        await session.start(_command(root, grok), cwd=str(tmp_path), signature="s")
        (turn,) = await asyncio.wait_for(_turns(session, "ORPHAN"), 8)  # the sleep it left lives 20 s
        await session.stop()
        return turn

    turn = asyncio.run(_run())
    assert (turn.result_event, turn.exit_code) == (None, 3)
    assert "SECRET-STDERR" not in caplog.text  # only how much Grok wrote is logged


def test_a_process_that_dies_mid_turn_is_a_turn_without_a_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _operator(tmp_path, monkeypatch)
    root, grok = tmp_path / "runtime", _fake(tmp_path)

    async def _run() -> tuple[Any, Events, bool]:
        events = Events()
        session = AcpGrok(label="manager")
        await session.start(_command(root, grok), cwd=str(tmp_path), signature="s")
        (turn,) = await _turns(session, "CRASH", events=events)
        stale = session.stale("s")
        await session.stop()
        return turn, events, stale

    turn, events, stale = _go(_run())
    assert (turn.result_event, turn.exit_code, turn.session_started) == (None, 3, True)
    assert stale  # the owner starts over
    assert events.kinds() == ["system:init"]
    assert "SECRET-STDERR" not in caplog.text


def test_lines_that_are_not_jsonrpc_messages_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _operator(tmp_path, monkeypatch)
    root, grok = tmp_path / "runtime", _fake(tmp_path)

    async def _run() -> Any:
        session = AcpGrok(label="manager")
        await session.start(_command(root, grok), cwd=str(tmp_path), signature="s")
        turns = await _turns(session, "GARBAGE", "after")
        await session.stop()
        return turns

    first, second = _go(_run())
    assert first.result_event["result"].endswith("| GARBAGE | turn 1")
    assert second.result_event["result"].endswith("| after | turn 2")
    assert "this is not json" not in caplog.text


def test_a_response_with_neither_or_both_of_result_and_error_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _operator(tmp_path, monkeypatch)
    root, grok = tmp_path / "runtime", _fake(tmp_path)

    async def _run() -> Any:
        session = AcpGrok(label="manager")
        await session.start(_command(root, grok), cwd=str(tmp_path), signature="s")
        (turn,) = await _turns(session, "BADRESP")
        await session.stop()
        return turn

    assert _go(_run()).result_event["result"].endswith("| BADRESP | turn 1")


def test_a_permission_request_is_refused_and_the_turn_goes_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _operator(tmp_path, monkeypatch)
    root, grok = tmp_path / "runtime", _fake(tmp_path)

    async def _run() -> Any:
        session = AcpGrok(label="manager")
        await session.start(_command(root, grok), cwd=str(tmp_path), signature="s")
        (turn,) = await _turns(session, "ASK")
        await session.stop()
        return turn

    turn = _go(_run())
    assert json.loads(turn.result_event["result"].removeprefix("permission outcome: ")) == {
        "outcome": {"outcome": "cancelled"}}


def test_an_error_response_is_an_error_result_and_the_session_stays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _operator(tmp_path, monkeypatch)
    root, grok = tmp_path / "runtime", _fake(tmp_path)

    async def _run() -> tuple[Any, bool]:
        session = AcpGrok(label="manager")
        await session.start(_command(root, grok), cwd=str(tmp_path), signature="s")
        (turn,) = await _turns(session, "ERROR")
        stale = session.stale("s")
        await session.stop()
        return turn, stale

    turn, stale = _go(_run())
    assert turn.result_event["is_error"] and turn.result_event["errors"] == ["model unavailable"]
    assert not stale


def test_a_session_grok_no_longer_has_starts_no_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _operator(tmp_path, monkeypatch)
    root, grok = tmp_path / "runtime", _fake(tmp_path)

    async def _run() -> tuple[Any, bool]:
        session = AcpGrok(label="manager")
        command = _command(root, grok) + GROK.resume_args("0199a000-0000-7000-8000-00000000dead")
        await session.start(command, cwd=str(tmp_path), signature="s")

        async def on_event(event: dict[str, Any]) -> None:
            raise AssertionError(f"no event expected, got {event}")

        turn = await session.run_turn("hello", on_event)
        stale = session.stale("s")
        await session.stop()
        return turn, stale

    turn, stale = _go(_run())
    assert (turn.result_event, turn.session_started, turn.exit_code) == (None, False, 1)
    assert stale


# --- When ACP is not there ---


def test_a_grok_without_agent_stdio_runs_the_session_per_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _operator(tmp_path, monkeypatch)
    root, grok = tmp_path / "runtime", _fake_with_headless(tmp_path)

    async def _run() -> tuple[Any, Any, str, Usage]:
        session = AcpGrok(label="manager")
        env = {**_launch().environment(), "FAKE_ACP_DEAD": "1"}
        await session.start(_command(root, grok), cwd=str(tmp_path), signature="s", env=env)
        assert session.transport == "acp"
        one, two = await _turns(session, "hello", "again")
        transport, totals = session.transport, session.totals
        await session.stop()
        return one, two, transport, totals

    one, two, transport, totals = _go(_run())
    assert transport == "per-turn"
    assert [one.result_event["result"], two.result_event["result"]] == [
        "prompt A | hello | turn 1", "prompt A | again | turn 2"]
    assert totals.output_tokens == 10


def test_a_saved_session_needs_loadsession_or_the_session_goes_on_per_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _operator(tmp_path, monkeypatch)
    root, grok = tmp_path / "runtime", _fake_with_headless(tmp_path)

    async def _run() -> tuple[Any, Any, str]:
        events = Events()
        session = AcpGrok(label="manager")
        await session.start(_command(root, grok), cwd=str(tmp_path), signature="s", env=_launch().environment())
        (one,) = await _turns(session, "hello", events=events)
        await session.stop()
        # Another Grok: it answers `initialize` without `loadSession`. The session was kept in the same home, and
        # the per-turn path resumes it there.
        env = {**_launch().environment(), "FAKE_ACP_NO_LOAD": "1"}
        command = _command(root, grok) + GROK.resume_args(events.seen[0]["session_id"])
        await session.start(command, cwd=str(tmp_path), signature="s", env=env)
        (two,) = await _turns(session, "again")
        transport = session.transport
        await session.stop()
        return one, two, transport

    one, two, transport = _go(_run())
    assert transport == "per-turn" and two.result_event["result"] == "prompt A | again | turn 2"
    assert one.result_event["result"] == "prompt A | hello | turn 1"


@pytest.mark.parametrize("variable,value", [("FAKE_ACP_LOAD_ERROR", "1"), ("FAKE_ACP_PROTOCOL", "2"),
                                            ("FAKE_ACP_PROTOCOL", "true")])
def test_a_load_error_that_is_not_a_missing_session_or_another_protocol_goes_on_per_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, variable: str, value: str,
) -> None:
    _operator(tmp_path, monkeypatch)
    root, grok = tmp_path / "runtime", _fake_with_headless(tmp_path)

    async def _run() -> tuple[Any, str]:
        events = Events()
        session = AcpGrok(label="manager")
        await session.start(_command(root, grok), cwd=str(tmp_path), signature="s", env=_launch().environment())
        await _turns(session, "hello", events=events)
        await session.stop()
        env = {**_launch().environment(), variable: value}
        command = _command(root, grok) + GROK.resume_args(events.seen[0]["session_id"])
        await session.start(command, cwd=str(tmp_path), signature="s", env=env)
        (two,) = await _turns(session, "again")
        transport = session.transport
        await session.stop()
        return two, transport

    two, transport = _go(_run())
    # The session is not discarded: the per-turn path resumes it.
    assert transport == "per-turn" and two.result_event["result"] == "prompt A | again | turn 2"
    assert "SECRET-LOAD-TEXT" not in caplog.text  # Grok's error text is not logged, only its code


def test_a_grok_that_does_not_answer_initialize_is_given_up_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _operator(tmp_path, monkeypatch)
    monkeypatch.setattr(grok_acp, "INIT_TIMEOUT_SEC", 0.5)
    root, grok = tmp_path / "runtime", _fake_with_headless(tmp_path)

    async def _run() -> tuple[str, Any]:
        session = AcpGrok(label="manager")
        env = {**_launch().environment(), "FAKE_ACP_SILENT": "1"}
        await session.start(_command(root, grok), cwd=str(tmp_path), signature="s", env=env)
        (turn,) = await _turns(session, "hello")
        transport = session.transport
        await session.stop()
        return transport, turn

    transport, turn = _go(_run())
    assert transport == "per-turn" and turn.result_event["result"] == "prompt A | hello | turn 1"


def test_a_command_with_no_acp_form_runs_per_turn_from_the_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _operator(tmp_path, monkeypatch)
    root = tmp_path / "runtime"
    grok = _fake_with_headless(tmp_path)

    async def _run() -> tuple[str, Any]:
        session = AcpGrok(label="manager")
        command = _command(root, grok)
        command.insert(command.index("-m"), "--sandbox")
        command.insert(command.index("-m"), "off")
        await session.start(command, cwd=str(tmp_path), signature="s", env=_launch().environment())
        (turn,) = await _turns(session, "hello")
        transport = session.transport
        await session.stop()
        return transport, turn

    transport, turn = _go(_run())
    assert transport == "per-turn" and turn.result_event["result"] == "prompt A | hello | turn 1"


def test_a_read_only_session_gets_an_agent_profile_with_the_reading_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _operator(tmp_path, monkeypatch)
    root, grok = tmp_path / "runtime", _fake(tmp_path)

    async def _run() -> None:
        session = AcpGrok(label="planner-task-1")
        await session.start(_command(root, grok, read_only=True), cwd=str(tmp_path), signature="s")
        await _turns(session, "hello")
        await session.stop()

    _go(_run())
    home = root / ".local" / "grok" / "sessions" / "planner-task-1"
    (run,) = _acp_runs(root)
    assert run["args"][run["args"].index("--agent-profile") + 1] == str((home / "acp-agent.md").absolute())
    assert "tools: read_file,grep,list_dir" in (home / "acp-agent.md").read_text()
    assert "--tools" not in run["args"]


def test_a_new_session_removes_the_owners_earlier_ones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _operator(tmp_path, monkeypatch)
    root, grok = tmp_path / "runtime", _fake(tmp_path)

    async def _run() -> None:
        session = AcpGrok(label="manager")
        for message in ("one", "two"):
            await session.start(_command(root, grok), cwd=str(tmp_path), signature="s")
            await _turns(session, message)
            await session.stop()  # the owner moved on: its next session is a new one

    _go(_run())
    (group,) = (root / ".local" / "grok" / "sessions" / "manager" / "sessions").iterdir()
    assert [p.name for p in group.iterdir()] == ["0199a000-0000-7000-8000-000000000002"]


def test_a_session_needs_its_executable(tmp_path: Path) -> None:
    async def _run() -> None:
        await AcpGrok(label="manager").start(_command(tmp_path, str(tmp_path / "missing-grok")), cwd=str(tmp_path),
                                             signature=None)

    with pytest.raises(OSError, match="was not found"):
        _go(_run())


def test_grok_serves_its_resident_sessions_over_acp() -> None:
    assert GROK.resident_transport is ResidentTransport.ACP
    assert isinstance(GROK.resident_session("manager"), AcpGrok)
    assert adapter("codex").resident_transport is ResidentTransport.PER_TURN
