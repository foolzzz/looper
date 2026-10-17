"""Launching the Claude Code CLI for an agent and recording its runs.

Covers reading stream-json output line by line with no line-length limit, the
flags that keep the operator's personal Claude Code configuration out of an
agent's run, the permission flags every agent but a Planner shares and a
Planner's read-only flags, the system prompt file flags, and the run record.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents.agent_settings import AgentType
from looper.agents.claude_cli import isolation_args, iter_lines, system_prompt_args


def _priced_launch() -> Any:
    """A Claude Code launch with a provider Looper prices: m-main at $2 input and $10 output per million tokens."""
    from looper.agents.agent_settings import AgentSettings, Launch
    from looper.agents.profile_registry import AgentProfile, CatalogModel, ProviderSpec, RuntimeSpec
    from looper.agents.runtimes import ApiProtocol, AuthKind, adapter

    provider = ProviderSpec("prov-x", "x", AuthKind.API_KEY, frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
                            base_url="https://example.invalid/anthropic",
                            catalog=(CatalogModel("m-main", input_usd_per_mtok=2.0, output_usd_per_mtok=10.0),))
    runtime = RuntimeSpec("rt-claude-code", "claude-code", "claude-code", "", adapter("claude-code").capabilities)
    return Launch(AgentSettings(profile=AgentProfile("agent-x", "claude-code/m-main", runtime, provider, "m-main")))


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
        from looper.agents.claude_cli import ClaudeRunRecorder

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
        assert "metadata" not in finish  # nothing refused, nothing checked: the record's metadata is as it was

    def test_the_calls_a_run_was_refused_and_how_a_turn_was_checked_are_kept(self, agent_db: Any) -> None:
        recorder = self._recorder()

        async def _run() -> None:
            await recorder.start()
            await recorder.finish(output_text="ok", result_event={
                "type": "result", "subtype": "success", "is_error": False, "permission_denials": [
                    {"tool_name": "Bash", "tool_use_id": "t", "tool_input": {"command": "touch made.txt"}}]},
                metadata={"read_only_check": "compared", "planner_changed_files": ["notes.md"]})

        asyncio.run(_run())
        assert agent_db.finish_record.await_args.kwargs["metadata"] == {
            "read_only_check": "compared", "planner_changed_files": ["notes.md"],
            "permission_denials": [{"tool": "Bash", "input": "touch made.txt", "id": "t"}]}

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

    @pytest.mark.parametrize(
        ("ends", "priced", "status", "error", "cost"),
        [
            ("cancelled", True, "interrupted", "stopped before it ended", 0.3),
            ("cancelled", False, "interrupted", "stopped before it ended", None),
            ("error", True, "failed", "ValueError: judging failed", 0.3),
            ("finished", True, "completed", None, 0.3),
        ],
    )
    def test_a_run_given_up_before_it_ends_closes_its_record_once_with_what_it_spent(
        self, agent_db: Any, ends: str, priced: bool, status: str, error: str | None, cost: float | None,
    ) -> None:
        """A run Looper stops (a kill, its task or the server stopping) ends ``interrupted``, one it stops following
        after an error ``failed``, with the tokens its requests used and their cost when Looper prices the run; a
        record the run closed itself is not closed again."""
        from looper.agents.claude_cli import ClaudeRunRecorder
        from looper.agents.run_usage import Usage
        from looper.agents.runtimes.pricing import usage_event

        recorder = ClaudeRunRecorder(agent_id="worker-1", task_id="task-1", command=["claude"], cwd="/tmp/proj",
                                     input_text="go", launch=_priced_launch() if priced else None)

        async def _run() -> asyncio.Task[None]:
            requested = asyncio.Event()

            async def run() -> None:
                async with recorder:
                    # $0.20 + $0.10 at the provider's prices
                    await recorder.record(usage_event("m-main", Usage(input_tokens=100_000, output_tokens=10_000)))
                    requested.set()
                    if ends == "finished":
                        await recorder.finish(output_text="done", result_event={
                            "type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.5,
                            "modelUsage": {"m-main": {"inputTokens": 100_000, "outputTokens": 10_000}}})
                    if ends == "cancelled":
                        await asyncio.sleep(60)
                    raise ValueError("judging failed")

            task = asyncio.create_task(run())
            await asyncio.wait_for(requested.wait(), 10)
            if ends == "cancelled":
                task.cancel()
            await asyncio.wait([task])
            return task

        task = asyncio.run(_run())
        assert task.cancelled() if ends == "cancelled" else isinstance(task.exception(), ValueError)
        (call,) = agent_db.finish_record.await_args_list
        finish = call.kwargs
        assert finish["status"] == status
        assert (error in finish["error"]) if error else finish["error"] is None
        assert (finish["input_tokens"], finish["output_tokens"]) == (100_000, 10_000)
        assert finish["cost_usd"] == (pytest.approx(cost) if cost is not None else None)

    def test_a_close_the_database_refuses_is_tried_once_more(self, agent_db: Any) -> None:
        agent_db.finish_record.side_effect = [ConnectionResetError("connection lost"), None]
        recorder = self._recorder()

        async def _run() -> None:
            await recorder.start()
            await recorder.finish(output_text="hi", result_event={"type": "result", "subtype": "success",
                                                                  "is_error": False, "total_cost_usd": 0.5})

        asyncio.run(_run())
        assert [(call.kwargs["status"], call.kwargs["cost_usd"]) for call in agent_db.finish_record.await_args_list] \
            == [("completed", 0.5)] * 2

    def test_a_stop_during_the_close_does_not_cut_the_write_short_and_a_hung_write_is_given_up(
        self, agent_db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from looper.agents import claude_cli

        written: list[str] = []

        async def slow_write(record_id: int, **fields: Any) -> None:
            await asyncio.sleep(0.3 if not written else 60)
            written.append(fields["status"])

        agent_db.finish_record.side_effect = slow_write
        monkeypatch.setattr(claude_cli, "CLOSE_TIMEOUT_SEC", 1.0)
        success = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.5}

        async def _run() -> tuple[bool, list[str], float]:
            recorder = self._recorder()
            await recorder.start()
            closing = asyncio.create_task(recorder.finish(output_text="hi", result_event=success))
            await asyncio.sleep(0.05)
            closing.cancel()  # the task stops while its record is being closed
            await asyncio.wait([closing])
            await asyncio.sleep(0.5)
            after_cancel = list(written)
            hung = self._recorder()
            await hung.start()
            began = asyncio.get_running_loop().time()
            await hung.finish(output_text=None, result_event=None)  # the database hangs: given up, never raised
            return closing.cancelled(), after_cancel, asyncio.get_running_loop().time() - began

        cancelled, after_cancel, waited = asyncio.run(_run())
        assert cancelled and after_cancel == ["completed"]
        assert 0.9 < waited < 5


def test_heartbeat_counts_partial_deltas_and_is_throttled(agent_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import claude_cli

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
    from looper.agents.claude_cli import ClaudeRunRecorder

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
    return [f"{directory}/CLAUDE.md", f"{directory}/.claude/CLAUDE.md", f"{directory}/.claude/rules/**",
            f"{directory}/AGENTS.md", f"{directory}/.claude/AGENTS.md"]


class TestIsolationArgs:
    def test_only_the_project_settings_load_and_no_mcp_server(self, tmp_path: Path) -> None:
        args = isolation_args(tmp_path)
        # User and local settings (with their hooks, plugins, skills, agents and
        # output style) stay out; the project's own settings and CLAUDE.md load.
        assert args[args.index("--setting-sources") + 1] == "project"
        assert "--strict-mcp-config" in args
        assert _settings(args)["autoMemoryEnabled"] is False

    @pytest.mark.parametrize("repository", [True, False])
    def test_memory_files_above_the_repository_are_excluded(self, tmp_path: Path, repository: bool) -> None:
        home = tmp_path / "home"
        repo = home / "repo"
        work = repo / "pkg" / "app"
        work.mkdir(parents=True)
        if repository:
            (repo / ".git").mkdir()

        excludes = _settings(isolation_args(work))["claudeMdExcludes"]

        # Claude Code reads CLAUDE.md, .claude/CLAUDE.md and .claude/rules/ in
        # every directory up to the root, and AGENTS.md where the project has no
        # CLAUDE.md; above the repository they are not the project's (the home
        # directory's .claude/ is the operator's own).
        # Without a repository the work directory is the project.
        project = (repo, repo / "pkg", work) if repository else (work,)
        above = (home, tmp_path, *tmp_path.parents) + (() if repository else (repo, repo / "pkg"))
        for directory in above:
            for pattern in _memory_patterns(directory):
                assert pattern.replace("//", "/") in excludes
        # The project's own memory files load, at its root and below.
        for directory in project:
            for pattern in _memory_patterns(directory):
                assert pattern not in excludes

    def test_the_operators_claude_directory_is_excluded_even_as_the_repository(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home = tmp_path / "home"
        (home / ".git").mkdir(parents=True)  # a home directory kept in git
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
        excludes = _settings(isolation_args(home))["claudeMdExcludes"]
        for directory in (home / ".claude", tmp_path / "config"):
            assert f"{directory}/CLAUDE.md" in excludes and f"{directory}/AGENTS.md" in excludes
            assert f"{directory}/rules/**" in excludes
        assert f"{home}/CLAUDE.md" not in excludes and f"{home}/AGENTS.md" not in excludes

    def test_a_symlinked_work_directory_is_covered_by_both_paths(self, tmp_path: Path) -> None:
        real = tmp_path / "real" / "work"
        real.mkdir(parents=True)
        (tmp_path / "link").symlink_to(tmp_path / "real")
        excludes = _settings(isolation_args(tmp_path / "link" / "work"))["claudeMdExcludes"]
        assert f"{tmp_path / 'link'}/CLAUDE.md" in excludes
        assert f"{(tmp_path / 'real').resolve()}/CLAUDE.md" in excludes


# --- What every agent may do ---

# Flags that would narrow an agent's tools or permissions again.
RESTRICTING_FLAGS = {"--tools", "--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools",
                     "--permission-mode"}


class TestPermissionArgs:
    @pytest.mark.parametrize("role", [None, AgentType.MANAGER, AgentType.WORKER, AgentType.REVIEWER])
    def test_every_launch_but_a_planners_carries_them_with_and_without_a_library(
        self, tmp_path: Path, role: AgentType | None,
    ) -> None:
        # The agents build their commands from a Launch, so the flags cannot diverge.
        from looper.agents.agent_settings import Launch
        from looper.agents.claude_cli import permission_args
        from looper.agents.library import AgentLibrary

        assert permission_args() == ["--dangerously-skip-permissions"]
        library = AgentLibrary(plugin_dirs=(str(tmp_path / "plugin"),))
        for launch in (Launch(role=role), Launch(library=library, role=role)):
            args = launch.args()
            assert args[:len(permission_args())] == permission_args()
            assert args.count("--dangerously-skip-permissions") == 1
            assert not RESTRICTING_FLAGS & set(args)
            assert "read_only" not in launch.record()

    def test_a_planner_runs_read_only_with_its_mcp_servers_skills_and_web_reads(self, tmp_path: Path) -> None:
        from looper.agents.agent_settings import Launch
        from looper.agents.library import AgentLibrary, ItemKind, LoadedItem

        library = AgentLibrary(items=(LoadedItem(ItemKind.MCP, "looper", "a" * 64, plugin="", scope="builtin"),
                                      LoadedItem(ItemKind.MCP, "docs", "b" * 64, plugin="", scope="global"),
                                      LoadedItem(ItemKind.SKILL, "deploy", "c" * 64, plugin="personal",
                                                 scope="global")),
                               mcp_config=str(tmp_path / "mcp.json"))
        launch = Launch(library=library, role=AgentType.PLANNER)
        args = launch.args()
        # Claude Code's default permission mode, pinned against a project's own settings, with no one to answer its
        # prompts, so that what it would ask about is refused; no file-editing tools.
        assert "--dangerously-skip-permissions" not in args
        assert args[:8] == ["--permission-mode", "manual", "--permission-prompts", "none",
                            "--disallowedTools", "Edit,Write,NotebookEdit",
                            "--allowedTools", "Skill,WebFetch,WebSearch,mcp__looper,mcp__docs"]
        assert launch.record()["read_only"] == "permissions"
        assert Launch(role=AgentType.PLANNER).args()[6:8] == ["--allowedTools", "Skill,WebFetch,WebSearch"]

    @pytest.mark.parametrize(("server", "rule"), [
        ("looper", "mcp__looper"), ("probe.v2", "mcp__probe_v2"), ("my server+x", "mcp__my_server_x"),
        ("a-b_c9", "mcp__a-b_c9"),
    ])
    def test_an_mcp_servers_rule_names_it_as_claude_code_names_its_tools(self, server: str, rule: str) -> None:
        from looper.agents.claude_cli import mcp_rule

        assert mcp_rule(server) == rule

    def test_a_refused_tool_call_is_read_from_the_result(self) -> None:
        from looper.agents.claude_cli import DENIAL_INPUT_CHARS, permission_denials

        result = {"type": "result", "permission_denials": [
            {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "echo hi > made.txt",
                                                                        "description": "Create made.txt"}},
            {"tool_name": "mcp__x__write", "tool_use_id": "t2", "tool_input": {"path": "a", "text": "x" * 500}},
            "not a denial",
        ]}
        denials = permission_denials(result)
        assert denials[0] == {"tool": "Bash", "input": "echo hi > made.txt", "id": "t1"}
        assert denials[1]["tool"] == "mcp__x__write" and len(denials[1]["input"]) == DENIAL_INPUT_CHARS
        assert len(denials) == 2
        assert permission_denials({"type": "result"}) == permission_denials(None) == []

def test_the_system_prompt_comes_from_its_file_and_is_read_again_on_resume(tmp_path: Path) -> None:
    path = tmp_path / "abc.md"
    # Without "--system-prompt-snapshot off", a resumed session keeps the
    # prompt of its first request and ignores a changed file.
    assert system_prompt_args(path) == [
        "--append-system-prompt-file", str(path), "--system-prompt-snapshot", "off",
    ]


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
    tmp_path: Path, fixture: str, messages: list[str],
) -> tuple[list[tuple[list[dict[str, Any]], Any]], list[tuple[_Observed, Any]], Any]:
    """Send ``messages`` to a resident stand-in replaying ``fixture``.

    Returns each user turn's events and outcome, each autonomous turn's
    ``_Observed`` and outcome, and the totals after the last result. Each
    message is its own turn's tag.
    """
    from claude_stream_replay import replay_script

    from looper.agents.claude_cli import ResidentClaude

    async def _run() -> Any:
        autonomous: list[tuple[_Observed, Any]] = []

        async def begin(after: Any) -> _Observed:
            return _Observed(autonomous, after)

        claude = ResidentClaude(label="test")
        await claude.start([replay_script(tmp_path, fixture)], cwd=str(tmp_path), signature=1, autonomous=begin)
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

    def test_a_handler_that_fails_never_breaks_the_users_turns(self, tmp_path: Path) -> None:
        from claude_stream_replay import replay_script

        from looper.agents.claude_cli import ResidentClaude

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
        from looper.agents.claude_cli import ResidentClaude

        fake = fake_claude(
            tmp_path / "claude",
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
            "time.sleep(30)  # still working on its own turn when stopped\n",
        )

        async def _run() -> list[Any]:
            ended: list[Any] = []
            begun = asyncio.Event()

            async def begin(after: Any) -> _Observed:
                begun.set()
                return _Observed(ended, after)

            async def on_event(event: dict[str, Any]) -> None:
                return None

            claude = ResidentClaude(label="test")
            await claude.start([fake], cwd=str(tmp_path), signature=1, autonomous=begin)
            await claude.run_turn("hi", on_event, tag="hi")
            await asyncio.wait_for(begun.wait(), 10)
            await claude.stop(kill=True)
            return ended

        ((observed, own),) = asyncio.run(_run())
        assert own.result_event is None and own.usage.cost_usd is None
        assert [e["subtype"] for e in observed.events] == ["task_notification", "init"]
        assert observed.after == "hi"
