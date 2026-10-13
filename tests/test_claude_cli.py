"""Reading Claude's stream-json output line by line, with no line-length limit."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from loopx.agents.claude_cli import iter_lines


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

    def test_records_the_appended_system_prompt(self, agent_db: Any) -> None:
        from loopx.agents.claude_cli import ClaudeRunRecorder

        recorder = ClaudeRunRecorder(
            agent_id="worker-1", task_id="task-1", cwd="/tmp/proj", input_text="go",
            command=["claude", "--append-system-prompt", "Be brief.\n\n## Project: calc\nUse uv."],
            system_prompt="Be brief.\n\n## Project: calc\nUse uv.",
        )
        asyncio.run(recorder.start())
        metadata = agent_db.start_record.await_args.kwargs["metadata"]
        # The exact argv stays as it was; the prompt is also kept on its own for display.
        assert metadata["command"] == ["claude", "--append-system-prompt", "Be brief.\n\n## Project: calc\nUse uv."]
        assert metadata["system_prompt"] == "Be brief.\n\n## Project: calc\nUse uv."

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
