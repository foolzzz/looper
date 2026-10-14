"""Launching the Claude Code CLI for an agent and recording its runs.

Covers reading stream-json output line by line with no line-length limit, the
flags that keep the operator's personal Claude Code configuration out of an
agent's run, the permission flags every agent shares, the system prompt file
flags, and the run record.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from loopx.agents.claude_cli import ISOLATION_ID, isolation_args, iter_lines, system_prompt_args


def _lines(*chunks: bytes) -> list[bytes]:
    async def _run() -> list[bytes]:
        reader = asyncio.StreamReader()
        for chunk in chunks:
            reader.feed_data(chunk)
        reader.feed_eof()
        return [line async for line in iter_lines(reader)]

    return asyncio.run(_run())


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        ((), []),
        ((b"a\nb\n",), [b"a\n", b"b\n"]),
        ((b"ab", b"c\nd", b"e\n"), [b"abc\n", b"de\n"]),
        ((b"a\n", b"tail"), [b"a\n", b"tail"]),
        ((b"\n\n",), [b"\n", b"\n"]),
    ],
)
def test_lines(chunks: tuple[bytes, ...], expected: list[bytes]) -> None:
    assert _lines(*chunks) == expected


def test_line_longer_than_any_reader_limit() -> None:
    # Larger than asyncio's 64 KiB default and the earlier 16 MiB cap.
    big = b"x" * (20 * 1024 * 1024)
    assert _lines(big[:7_000_000], big[7_000_000:] + b"\nnext\n") == [big + b"\n", b"next\n"]


class TestClaudeRunRecorder:
    def _recorder(self) -> Any:
        from loopx.agents.claude_cli import ClaudeRunRecorder

        return ClaudeRunRecorder(
            agent_id="worker-1", task_id="task-1", command=["claude", "--print", "go"],
            cwd="/tmp/proj", input_text="go",
        )

    def test_records_command_events_and_outcome(self, agent_db: Any) -> None:
        recorder = self._recorder()

        async def _run() -> None:
            await recorder.start()
            await recorder.record({"type": "system", "subtype": "init", "model": "claude-x",
                                   "session_id": "s-1", "tools": ["t" * 50] * 100})
            await recorder.record({"type": "stream_event", "event": {"type": "content_block_delta"}})
            await recorder.record({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
            await recorder.finish(output_text="hi", result_event={
                "type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.5,
                "usage": {"input_tokens": 12, "output_tokens": 3},
            })

        asyncio.run(_run())
        start = agent_db.start_record.await_args.kwargs
        assert start["metadata"] == {"command": ["claude", "--print", "go"], "cwd": "/tmp/proj"}
        assert (start["agent_id"], start["task_id"], start["input_text"]) == ("worker-1", "task-1", "go")
        appended = [call.args[1][0] for call in agent_db.append_events.await_args_list]
        assert [seq for seq, _ in appended] == [1, 2]  # partial deltas are not stored
        assert appended[0][1]["tools"] == "<100 items>"
        assert appended[1][1]["type"] == "assistant"
        finish = agent_db.finish_record.await_args.kwargs
        assert (finish["status"], finish["output_text"], finish["model"], finish["session_id"]) == (
            "completed", "hi", "claude-x", "s-1")
        assert (finish["input_tokens"], finish["output_tokens"], finish["cost_usd"]) == (12, 3, 0.5)
        assert finish["error"] is None

    @pytest.mark.parametrize(
        ("result_event", "error", "expected"),
        [
            ({"type": "result", "subtype": "error_max_turns", "is_error": True}, None, "error_max_turns"),
            (None, None, "no result"),
            ({"type": "result", "subtype": "success"}, "report does not match", "report does not match"),
        ],
    )
    def test_failed_runs_carry_a_reason(self, agent_db: Any, result_event: Any, error: Any, expected: str) -> None:
        recorder = self._recorder()

        async def _run() -> None:
            await recorder.start()
            await recorder.finish(output_text=None, result_event=result_event, error=error)

        asyncio.run(_run())
        finish = agent_db.finish_record.await_args.kwargs
        assert finish["status"] == "failed"
        assert expected in finish["error"]

    def test_records_the_system_prompt_its_hash_and_its_layers(self, agent_db: Any) -> None:
        from loopx.agents.claude_cli import ClaudeRunRecorder
        from loopx.agents.system_prompt import SystemPrompt

        text = "Be brief.\n\n## Project: calc\nUse uv."
        layers = {"role": {"config_id": "worker"}, "isolation": ISOLATION_ID}
        command = ["claude", "--append-system-prompt-file", "/rt/.local/prompts/abc.md"]
        recorder = ClaudeRunRecorder(
            agent_id="worker-1", task_id="task-1", cwd="/tmp/proj", input_text="go",
            command=command, system_prompt=SystemPrompt(text, layers),
        )
        asyncio.run(recorder.start())
        metadata = agent_db.start_record.await_args.kwargs["metadata"]
        # The argv stays as it was; the prompt itself, its hash and its layers are kept beside it.
        assert metadata["command"] == command
        assert metadata["system_prompt"] == text
        assert metadata["system_prompt_sha256"] == hashlib.sha256(text.encode()).hexdigest()
        assert metadata["prompt_layers"] == layers

    def test_database_errors_never_fail_the_run(self, agent_db: Any) -> None:
        agent_db.start_record.side_effect = RuntimeError("database pool not initialised")
        recorder = self._recorder()

        async def _run() -> None:
            await recorder.start()
            await recorder.record({"type": "assistant", "message": {"content": []}})
            await recorder.finish(output_text="x", result_event=None)

        asyncio.run(_run())
        agent_db.append_events.assert_not_awaited()
        agent_db.finish_record.assert_not_awaited()


def test_heartbeat_counts_partial_deltas_and_is_throttled(agent_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from loopx.agents import claude_cli

    clock = iter([0.0, 1.0, 2.0, 15.0, 16.0, 17.0])
    monkeypatch.setattr(claude_cli, "_clock", lambda: next(clock))
    recorder = claude_cli.ClaudeRunRecorder(
        agent_id="worker-1", task_id=None, command=["claude"], cwd="/tmp", input_text="go",
    )

    async def _run() -> None:
        await recorder.start()                                   # t=0 (constructor read 0.0)
        delta = {"type": "stream_event", "event": {"type": "content_block_delta"}}
        await recorder.record(delta)                             # t=1 → first heartbeat
        await recorder.record(delta)                             # t=2 → throttled
        await recorder.record(delta)                             # t=15 → heartbeat again

    asyncio.run(_run())
    assert agent_db.touch_record.await_count == 2
    agent_db.append_events.assert_not_awaited()  # deltas keep the run alive but are not stored



def test_thinking_progress_is_one_row_per_thinking_phase(agent_db: Any) -> None:
    from loopx.agents.claude_cli import ClaudeRunRecorder

    def thinking(tokens: int) -> dict[str, Any]:
        return {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": tokens}

    recorder = ClaudeRunRecorder(agent_id="worker-1", task_id=None, command=["claude"], cwd="/tmp", input_text="go")

    async def _run() -> None:
        await recorder.start()
        for event in (
            thinking(100), thinking(200), thinking(300),                     # phase 1 → row 1, updated twice
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},  # row 2
            thinking(50), thinking(80),                                       # phase 2 → row 3, updated once
        ):
            await recorder.record(event)

    asyncio.run(_run())
    appended = [call.args[1][0] for call in agent_db.append_events.await_args_list]
    assert [(seq, e.get("subtype") or e["type"]) for seq, e in appended] == [
        (1, "thinking_tokens"), (2, "assistant"), (3, "thinking_tokens"),
    ]
    updates = [(call.args[1], call.args[2]["estimated_tokens"]) for call in agent_db.update_event.await_args_list]
    assert updates == [(1, 200), (1, 300), (3, 80)]


# --- The flags that keep the operator's personal Claude Code configuration out ---


def _settings(args: list[str]) -> dict[str, Any]:
    return json.loads(args[args.index("--settings") + 1])


def _memory_patterns(directory: Path) -> list[str]:
    return [f"{directory}/CLAUDE.md", f"{directory}/.claude/CLAUDE.md", f"{directory}/.claude/rules/**"]


class TestIsolationArgs:
    def test_only_the_project_settings_load_and_no_mcp_server(self, tmp_path: Path) -> None:
        args = isolation_args(tmp_path)
        # User and local settings (with their hooks, plugins, skills, agents and
        # output style) stay out; the project's own settings and CLAUDE.md load.
        assert args[args.index("--setting-sources") + 1] == "project"
        assert "--strict-mcp-config" in args
        assert _settings(args)["autoMemoryEnabled"] is False

    def test_memory_files_above_the_repository_are_excluded(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        repo = home / "repo"
        (repo / ".git").mkdir(parents=True)
        work = repo / "pkg" / "app"
        work.mkdir(parents=True)

        excludes = _settings(isolation_args(work))["claudeMdExcludes"]

        # Claude Code reads CLAUDE.md, .claude/CLAUDE.md and .claude/rules/ in
        # every directory up to the root; above the repository they are not the
        # project's (the home directory's .claude/ is the operator's own).
        for directory in (home, tmp_path, *tmp_path.parents):
            for pattern in _memory_patterns(directory):
                assert pattern.replace("//", "/") in excludes
        # The repository's own memory files load, at its root and below.
        for directory in (repo, repo / "pkg", work):
            for pattern in _memory_patterns(directory):
                assert pattern not in excludes

    def test_without_a_repository_the_work_directory_is_the_project(self, tmp_path: Path) -> None:
        work = tmp_path / "a" / "b"
        work.mkdir(parents=True)
        excludes = _settings(isolation_args(work))["claudeMdExcludes"]
        assert f"{tmp_path / 'a'}/CLAUDE.md" in excludes
        assert f"{work}/CLAUDE.md" not in excludes

    def test_the_operators_claude_directory_is_excluded_even_as_the_repository(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "home"
        (home / ".git").mkdir(parents=True)  # a home directory kept in git
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
        excludes = _settings(isolation_args(home))["claudeMdExcludes"]
        for directory in (home / ".claude", tmp_path / "config"):
            assert f"{directory}/CLAUDE.md" in excludes
            assert f"{directory}/rules/**" in excludes
        assert f"{home}/CLAUDE.md" not in excludes

    def test_a_symlinked_work_directory_is_covered_by_both_paths(self, tmp_path: Path) -> None:
        real = tmp_path / "real" / "work"
        real.mkdir(parents=True)
        (tmp_path / "link").symlink_to(tmp_path / "real")
        excludes = _settings(isolation_args(tmp_path / "link" / "work"))["claudeMdExcludes"]
        assert f"{tmp_path / 'link'}/CLAUDE.md" in excludes
        assert f"{(tmp_path / 'real').resolve()}/CLAUDE.md" in excludes

    def test_the_same_directory_gives_the_same_flags(self, tmp_path: Path) -> None:
        # A resident agent compares its command between turns; it must not change by itself.
        assert isolation_args(tmp_path) == isolation_args(str(tmp_path))
        assert isinstance(ISOLATION_ID, str) and ISOLATION_ID


# --- What every agent may do ---

# Flags that would narrow an agent's tools or permissions again.
RESTRICTING_FLAGS = {"--tools", "--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools",
                     "--permission-mode"}


class TestPermissionArgs:
    def test_every_agent_skips_permission_prompts(self) -> None:
        from loopx.agents.claude_cli import permission_args

        assert permission_args() == ["--dangerously-skip-permissions"]
        assert not RESTRICTING_FLAGS & set(permission_args())

    def test_every_launch_carries_them_with_and_without_a_library(self, tmp_path: Path) -> None:
        # The four agents build their commands from a Launch, so the flags cannot diverge.
        from loopx.agents.agent_settings import BUILTIN_DEFAULTS, resolve_launch
        from loopx.agents.claude_cli import permission_args
        from loopx.agents.library import AgentLibrary

        library = AgentLibrary(plugin_dirs=(str(tmp_path / "plugin"),))
        for launch in (resolve_launch(BUILTIN_DEFAULTS), resolve_launch(BUILTIN_DEFAULTS, library=library)):
            args = launch.args()
            assert args[:len(permission_args())] == permission_args()
            assert args.count("--dangerously-skip-permissions") == 1
            assert not RESTRICTING_FLAGS & set(args)

def test_the_system_prompt_comes_from_its_file_and_is_read_again_on_resume(tmp_path: Path) -> None:
    path = tmp_path / "abc.md"
    # Without "--system-prompt-snapshot off", a resumed session keeps the
    # prompt of its first request and ignores a changed file.
    assert system_prompt_args(path) == [
        "--append-system-prompt-file", str(path), "--system-prompt-snapshot", "off",
    ]


@pytest.mark.parametrize(("script", "started"), [
    # What Claude 2.1.285 does when told to --resume a session it no longer has.
    ("import json, sys; print(json.dumps({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, "
     "'num_turns': 0, 'errors': ['No conversation found with session ID: x']}), flush=True)", False),
    ("import sys; sys.stderr.write('No conversation found with session ID: x\\n'); sys.exit(1)", False),
    ("import json, sys; print(json.dumps({'type': 'system', 'subtype': 'init'}), flush=True); sys.exit(3)", True),
])
def test_a_turn_that_never_started_a_session_is_told_apart(tmp_path: Path, script: str, started: bool) -> None:
    """A turn without Claude's init event never got a session going (an unknown --resume id)."""
    import sys

    from loopx.agents.claude_cli import ResidentClaude

    fake = tmp_path / "claude"
    fake.write_text(f"#!{sys.executable}\n{script}\n")
    fake.chmod(0o755)

    async def _run() -> Any:
        claude = ResidentClaude(label="test")
        await claude.start([str(fake)], cwd=str(tmp_path), signature=("s",))

        async def on_event(event: dict[str, Any]) -> None:
            return None

        turn = await claude.run_turn("hello", on_event)
        await claude.stop()
        return turn

    assert asyncio.run(_run()).session_started is started


# --- Turns Claude runs on its own ---
#
# Recorded from Claude Code 2.1.285 (tests/claude_stream_replay.py): after
# background work started in a turn ends, Claude runs a turn of its own and
# writes a result that answers no user message.


class _Observed:
    """An owner's view of one autonomous turn: its events and how it ended."""

    def __init__(self, ended: list[Any], after: Any) -> None:
        self.events: list[dict[str, Any]] = []
        self.after = after  # the tag of the user turn Claude ran before it
        self._ended = ended

    async def event(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    async def end(self, turn: Any) -> None:
        self._ended.append((self, turn))


def _resident_session(
    tmp_path: Path, fixture: str, messages: list[str], *, observe: bool = True,
) -> tuple[list[tuple[list[dict[str, Any]], Any]], list[tuple[_Observed, Any]], Any]:
    """Send ``messages`` to a resident stand-in replaying ``fixture``.

    Returns each user turn's events and outcome, each autonomous turn's
    ``_Observed`` and outcome (when ``observe``), and the totals after the
    last result. Each message is its own turn's tag.
    """
    from claude_stream_replay import replay_script

    from loopx.agents.claude_cli import ResidentClaude

    async def _run() -> Any:
        autonomous: list[tuple[_Observed, Any]] = []

        async def begin(after: Any) -> _Observed:
            return _Observed(autonomous, after)

        claude = ResidentClaude(label="test")
        await claude.start([replay_script(tmp_path, fixture)], cwd=str(tmp_path), signature=1,
                           **({"autonomous": begin} if observe else {}))
        turns = []
        for text in messages:
            seen: list[dict[str, Any]] = []

            async def on_event(event: dict[str, Any], seen: list[dict[str, Any]] = seen) -> None:
                seen.append(event)

            turns.append((seen, await claude.run_turn(text, on_event, tag=text)))
        await claude.stop()  # the stand-in has nothing left to write; stop() reads to its end
        return turns, autonomous, claude.totals

    return asyncio.run(_run())


def _kinds(events: list[dict[str, Any]]) -> list[str]:
    return [f"{e['type']}/{e.get('subtype') or e.get('state') or ('replay' if e.get('isReplay') else '')}"
            for e in events]


class TestAutonomousTurns:
    def test_a_background_subagents_turn_is_not_the_next_messages(self, tmp_path: Path) -> None:
        turns, autonomous, totals = _resident_session(tmp_path, "background_agent", ["start it", "7*6?"])
        (first_events, first), (second_events, second) = turns
        assert (first.result_event["result"], second.result_event["result"]) == ("LAUNCHED", "42")
        assert first.session_started and second.session_started
        # The turn Claude ran on its own went to the owner, with the subagent's events before it.
        ((observed, own),) = autonomous
        events = observed.events
        assert observed.after == "start it"  # the message Claude answered before it
        assert own.result_event["origin"] == {"kind": "task-notification", "producer": "session-task"}
        assert own.result_event["result"].startswith("Background task completed with result")
        # Its events start with what the subagent and Claude wrote between the turns.
        assert _kinds(events) == [
            "assistant/", "assistant/", "system/task_progress", "system/task_started", "system/task_notification",
            "user/", "assistant/", "assistant/", "system/background_tasks_changed", "system/task_updated",
            "system/task_notification", "system/init", "system/status", "assistant/", "assistant/", "result/success",
        ]
        assert all(e.get("parent_tool_use_id") for e in events[:2])
        # Nothing of it reached the second message's turn, which starts with that message's own events.
        assert _kinds(second_events)[:5] == ["command_lifecycle/queued", "command_lifecycle/started", "system/init",
                                             "system/status", "user/replay"]
        assert second_events[4]["message"]["content"] == "7*6?"
        assert not any(e.get("parent_tool_use_id") or e.get("subtype") == "task_notification" for e in second_events)
        # Each turn's own cost: Claude's running totals, taken in the order Claude wrote them.
        assert first.usage.cost_usd == pytest.approx(0.0130063)
        assert own.usage.cost_usd == pytest.approx(0.03359615 - 0.0130063)
        assert second.usage.cost_usd == pytest.approx(0.03617515 - 0.03359615)
        assert totals.cost_usd == pytest.approx(0.03617515)

    def test_a_message_queued_behind_an_autonomous_turn_starts_when_claude_starts_it(self, tmp_path: Path) -> None:
        turns, autonomous, _ = _resident_session(
            tmp_path, "background_task_race", ["bg 1", "fg 9", "bg 2", "9+1?"],
        )
        assert [turn.result_event["result"] for _, turn in turns] == ["STARTED", "FG-DONE", "STARTED-2", "10"]
        # A notification that came during a turn is part of that turn: Claude answered with it.
        folded = [e for e in turns[1][0] if e.get("isReplay") and e.get("origin")]
        assert len(folded) == 1 and folded[0] not in autonomous[0][0].events
        # The last message was queued while Claude ran a turn of its own; that turn is not the message's.
        ((observed, own),) = autonomous
        events = observed.events
        assert observed.after == "bg 2"  # Claude had not started "9+1?" yet
        assert own.result_event["result"] == "Background task BG-RACE-2 completed."
        assert _kinds(events)[:4] == ["system/background_tasks_changed", "system/task_updated",
                                      "system/task_notification", "system/init"]
        last_events, last = turns[3]
        assert _kinds(last_events)[:3] == ["command_lifecycle/queued", "command_lifecycle/started", "system/init"]
        assert last_events[-1] is last.result_event and last.session_started
        assert not any(e.get("isSynthetic") for e in last_events)
        assert [turn.usage.cost_usd for _, turn in turns] == pytest.approx(
            [0.0119405, 0.0181636 - 0.0119405, 0.0242396 - 0.0181636, 0.0347972 - 0.0317316])
        assert own.usage.cost_usd == pytest.approx(0.0317316 - 0.0242396)

    def test_without_an_owner_for_autonomous_turns_each_turn_still_costs_its_own_share(self, tmp_path: Path) -> None:
        turns, _, totals = _resident_session(tmp_path, "background_agent", ["start it", "7*6?"], observe=False)
        assert turns[1][1].result_event["result"] == "42"
        assert turns[1][1].usage.cost_usd == pytest.approx(0.03617515 - 0.03359615)
        assert totals.cost_usd == pytest.approx(0.03617515)

    def test_the_turns_message_carries_the_uuid_claude_marks_it_with(self, tmp_path: Path) -> None:
        turns, _, _ = _resident_session(tmp_path, "background_agent", ["start it", "7*6?"])
        for events, turn in turns:
            started = next(e for e in events if e["type"] == "command_lifecycle" and e["state"] == "started")
            replayed = next(e for e in events if e.get("isReplay"))
            assert started["command_uuid"] == replayed["uuid"] == turn.result_event["user_message_uuid"]

    def test_a_handler_that_fails_never_breaks_the_users_turns(self, tmp_path: Path) -> None:
        from claude_stream_replay import replay_script

        from loopx.agents.claude_cli import ResidentClaude

        class Broken:
            async def event(self, event: dict[str, Any]) -> None:
                raise RuntimeError("database down")

            async def end(self, turn: Any) -> None:
                raise RuntimeError("database down")

        async def _run() -> list[Any]:
            async def begin(after: Any) -> Broken:
                return Broken()

            async def on_event(event: dict[str, Any]) -> None:
                return None

            claude = ResidentClaude(label="test")
            await claude.start([replay_script(tmp_path, "background_agent")], cwd=str(tmp_path), signature=1,
                               autonomous=begin)
            turns = [await claude.run_turn(text, on_event) for text in ("start it", "7*6?")]
            await claude.stop()
            return turns

        first, second = asyncio.run(_run())
        assert (first.result_event["result"], second.result_event["result"]) == ("LAUNCHED", "42")
        assert second.usage.cost_usd == pytest.approx(0.03617515 - 0.03359615)

    def test_a_turn_of_its_own_that_stop_cuts_short_ends_without_a_result(self, tmp_path: Path) -> None:
        import sys

        from loopx.agents.claude_cli import ResidentClaude

        fake = tmp_path / "claude"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import json, sys, time\n"
            "line = json.loads(sys.stdin.readline())\n"
            "def emit(event): print(json.dumps(event), flush=True)\n"
            "for state in ('queued', 'started'):\n"
            "    emit({'type': 'command_lifecycle', 'command_uuid': line['uuid'], 'state': state})\n"
            "emit({'type': 'system', 'subtype': 'init', 'session_id': 's'})\n"
            "emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'ok', 'total_cost_usd': 0.5,\n"
            "      'user_message_uuid': line['uuid']})\n"
            "emit({'type': 'system', 'subtype': 'task_notification', 'status': 'completed'})\n"
            "emit({'type': 'system', 'subtype': 'init', 'session_id': 's'})\n"
            "sys.stdin.read()\n"
            "time.sleep(30)  # still working on its own turn when stopped\n"
        )
        fake.chmod(0o755)

        async def _run() -> list[Any]:
            ended: list[Any] = []
            begun = asyncio.Event()

            async def begin(after: Any) -> _Observed:
                begun.set()
                return _Observed(ended, after)

            async def on_event(event: dict[str, Any]) -> None:
                return None

            claude = ResidentClaude(label="test")
            await claude.start([str(fake)], cwd=str(tmp_path), signature=1, autonomous=begin)
            await claude.run_turn("hi", on_event, tag="hi")
            await asyncio.wait_for(begun.wait(), 10)
            await claude.stop(kill=True)
            return ended

        ((observed, own),) = asyncio.run(_run())
        assert own.result_event is None and own.usage.cost_usd is None
        assert [e["subtype"] for e in observed.events] == ["task_notification", "init"]
        assert observed.after == "hi"
