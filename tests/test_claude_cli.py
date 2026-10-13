"""Launching the Claude Code CLI for an agent and recording its runs.

Covers reading stream-json output line by line with no line-length limit, the
flags that keep the operator's personal Claude Code configuration out of an
agent's run, the system prompt file flags, and the run record.
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


def test_the_system_prompt_comes_from_its_file_and_is_read_again_on_resume(tmp_path: Path) -> None:
    path = tmp_path / "abc.md"
    # Without "--system-prompt-snapshot off", a resumed session keeps the
    # prompt of its first request and ignores a changed file.
    assert system_prompt_args(path) == [
        "--append-system-prompt-file", str(path), "--system-prompt-snapshot", "off",
    ]
