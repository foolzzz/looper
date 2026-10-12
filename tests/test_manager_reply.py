"""The Manager must always answer a chat message, even when Claude fails."""

from __future__ import annotations

import asyncio
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
    script.write_text(f"#!{sys.executable}\nimport json, os, sys, time\n{body}\n")
    script.chmod(0o755)
    return str(script)


def _ask(body: str) -> tuple[str, int | None]:
    """Send one chat message to a Manager backed by a fake claude; return its final reply."""

    async def _run() -> tuple[str, int | None]:
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
            queue = bus.get_queue(WEB.agent_id)
            while not queue.empty():
                envelope = queue.get_nowait()
                if envelope.message_type == MessageType.AGENT_REPLY:
                    replies.append(envelope)
            await bus.shutdown()
            assert len(replies) == 1, "the Manager must send exactly one final reply"
            assert replies[0].correlation_id == "corr-1"
            pid_file = root / "pid"
            pid = int(pid_file.read_text()) if pid_file.exists() else None
            return replies[0].payload["text"], pid

    return asyncio.run(_run())


@pytest.mark.usefixtures("agent_db")
class TestManagerReply:
    def test_long_stream_json_line_is_read(self) -> None:
        # Claude's init line lists every tool and skill; it can exceed 64 KiB.
        text, _ = _ask(
            "print(json.dumps({'type': 'system', 'subtype': 'init', 'tools': ['t' * 200_000]}))\n"
            "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'hi there'}]}}))\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'hi there'}))"
        )
        assert text == "hi there"

    def test_empty_run_is_reported(self) -> None:
        text, _ = _ask("sys.exit(1)")
        assert "Manager" in text and "1" in text

    def test_read_failure_is_reported_and_claude_is_killed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from loopx.agents.tiers import manager as manager_module

        monkeypatch.setattr(manager_module, "STREAM_LINE_LIMIT", 1024)
        text, pid = _ask(
            "open('pid', 'w').write(str(os.getpid()))\n"
            "print('x' * 4096, flush=True)\n"
            "time.sleep(30)"
        )
        assert "Manager" in text
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)  # type: ignore[arg-type]
