"""The Manager must always answer a chat message, even when Claude fails."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from loopx.agents.bus import MessageBus
from loopx.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from loopx.agents.tiers.manager import ManagerAgent, ManagerConfig

WEB = channel_address("web")


def _fake_claude(tmp: Path, body: str) -> str:
    """A stand-in `claude` binary that ignores its arguments."""
    script = tmp / "fake-claude"
    script.write_text(
        f"#!{sys.executable}\nimport json, os, sys, time\n"
        "open('argv.json', 'w').write(json.dumps(sys.argv[1:]))\n"
        f"{body}\n"
    )
    script.chmod(0o755)
    return str(script)


def _ask(body: str) -> dict[str, Any]:
    """Send one chat message to a Manager backed by a fake claude.

    Returns the final reply ``text``, the streamed ``deltas``, the fake's
    ``argv`` and, when the fake wrote one, its ``pid``.
    """

    async def _run() -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bus = MessageBus(runtime_root=root)
            manager = ManagerAgent(
                config=ManagerConfig(claude_bin=_fake_claude(root, body), work_dir=root),
                runtime_root=root,
            )
            manager._bus = bus
            await manager._handle(MessageEnvelope.create(
                source=WEB,
                target=MANAGER_ADDRESS,
                message_type=MessageType.USER_MESSAGE,
                payload={"text": "hello"},
                correlation_id="corr-1",
            ))
            replies = []
            deltas = []
            queue = bus.get_queue(WEB.agent_id)
            while not queue.empty():
                envelope = queue.get_nowait()
                if envelope.message_type == MessageType.AGENT_REPLY:
                    replies.append(envelope)
                elif envelope.message_type == MessageType.STATUS_UPDATE and envelope.payload.get("delta"):
                    deltas.append(envelope.payload["text"])
            await bus.shutdown()
            assert len(replies) == 1, "the Manager must send exactly one final reply"
            assert replies[0].correlation_id == "corr-1"
            pid_file = root / "pid"
            return {
                "text": replies[0].payload["text"],
                "deltas": deltas,
                "argv": json.loads((root / "argv.json").read_text()),
                "pid": int(pid_file.read_text()) if pid_file.exists() else None,
            }

    return asyncio.run(_run())


@pytest.mark.usefixtures("agent_db")
class TestManagerReply:
    def test_long_stream_json_line_is_read(self) -> None:
        # Claude's init line lists every tool and skill; it can exceed 64 KiB.
        text = _ask(
            "print(json.dumps({'type': 'system', 'subtype': 'init', 'tools': ['t' * 200_000]}))\n"
            "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'hi there'}]}}))\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'hi there'}))"
        )["text"]
        assert text == "hi there"

    def test_reply_is_recorded(self, agent_db: Any) -> None:
        _ask(
            "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'claude-x', 'session_id': 's-9'}))\n"
            "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'hi'}]}}))\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'hi'}))"
        )
        start = agent_db.start_record.await_args.kwargs
        assert start["agent_id"] == "manager" and start["input_text"] == "hello"
        assert "--append-system-prompt" in start["metadata"]["command"]
        finish = agent_db.finish_record.await_args.kwargs
        assert (finish["status"], finish["output_text"], finish["model"], finish["session_id"]) == (
            "completed", "hi", "claude-x", "s-9")

    def test_empty_run_is_reported(self) -> None:
        text = _ask("sys.exit(1)")["text"]
        assert "Manager" in text and "1" in text

    def test_read_failure_is_reported_and_claude_is_killed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from loopx.agents.tiers import manager as manager_module

        async def _broken_lines(stream: Any) -> Any:
            yield await stream.readline()
            raise OSError("pipe broke")

        monkeypatch.setattr(manager_module, "iter_lines", _broken_lines)
        reply = _ask(
            "open('pid', 'w').write(str(os.getpid()))\n"
            "print('{}', flush=True)\n"
            "time.sleep(30)"
        )
        text, pid = reply["text"], reply["pid"]
        assert "pipe broke" in text
        assert "Manager" in text
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)  # type: ignore[arg-type]


def _delta(text: str, parent: str | None = None) -> str:
    event = {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        "parent_tool_use_id": parent,
    }
    return f"print(json.dumps({event!r}), flush=True)\n"


def _message(text: str, parent: str | None = None) -> str:
    event = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
        "parent_tool_use_id": parent,
    }
    return f"print(json.dumps({event!r}), flush=True)\n"


_RESULT = "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False}))\n"


@pytest.mark.usefixtures("agent_db")
class TestManagerStreaming:
    def test_reply_streams_token_by_token(self) -> None:
        reply = _ask(_delta("杭州") + _delta("是浙江") + _delta("的省会。") + _message("杭州是浙江的省会。") + _RESULT)
        assert "--include-partial-messages" in reply["argv"]
        # Each partial chunk is pushed once; the full message is not pushed again.
        assert reply["deltas"] == ["杭州", "是浙江", "的省会。"]
        assert reply["text"] == "杭州是浙江的省会。"

    def test_whole_messages_still_stream_without_partials(self) -> None:
        reply = _ask(_message("first part. ") + _message("second part.") + _RESULT)
        assert reply["deltas"] == ["first part. ", "second part."]
        assert reply["text"] == "first part. second part."

    def test_subagent_chunks_are_not_streamed(self) -> None:
        reply = _ask(
            _delta("internal note", parent="toolu_1")
            + _message("internal note", parent="toolu_1")
            + _delta("答案")
            + _message("答案")
            + _RESULT
        )
        assert reply["deltas"] == ["答案"]
        assert reply["text"] == "答案"
