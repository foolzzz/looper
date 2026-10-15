"""The Lark channel's lark-cli commands, as lark-cli 1.0.97 takes them: a stand-in ``lark-cli`` on PATH accepts
only those (and answers anything else as lark-cli does, with a JSON error on stderr and exit 2), so nothing
reaches a real chat or user."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.lark import LarkChannelAdapter, consume_command, send_command
from looper.agents.message import MessageEnvelope, MessageType, channel_address

OPEN_ID = "ou_abcdefgh12"

# Like lark-cli 1.0.97: the two commands the adapter runs, and a validation error for anything else.
_STAND_IN = """\
import json, sys
args = sys.argv[1:]
with open({log!r}, "a") as log:
    log.write(json.dumps(args) + "\\n")
def refuse(message):
    print(json.dumps({{"ok": False, "error": {{"type": "validation", "message": message}}}}), file=sys.stderr)
    sys.exit(2)
if args[:4] == ["im", "+messages-send", "--as", "bot"]:
    rest = args[4:]
    if len(rest) != 4 or rest[0] not in ("--chat-id", "--user-id") or rest[2] != "--text":
        refuse("specify at least one of --chat-id or --user-id")
    if rest[1].endswith("gone"):
        refuse("Bot/User can NOT be out of the chat")
    print(json.dumps({{"ok": True}}))
elif args == ["event", "consume", "im.message.receive_v1", "--as", "bot"]:
    print(json.dumps({event}), flush=True)
else:
    refuse('unknown command "%s" for "lark-cli"' % args[0])
"""


@pytest.fixture
def lark_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put the stand-in first on PATH; returns the file it logs each argv to."""
    log = tmp_path / "lark-cli.jsonl"
    event = {"type": "im.message.receive_v1", "chat_id": "oc_team", "chat_type": "group", "message_id": "om_9",
             "message_type": "text", "sender_id": OPEN_ID, "sender_type": "user", "content": "@Looper status please"}
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude(bin_dir / "lark-cli", _STAND_IN.format(log=str(log), event=event))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def _calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def _adapter(tmp_path: Path) -> LarkChannelAdapter:
    return LarkChannelAdapter(app_id="cli_test", runtime_root=tmp_path,
                              allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))


def test_the_commands() -> None:
    assert send_command("hi", chat_id="oc_1") == ["lark-cli", "im", "+messages-send", "--as", "bot",
                                                  "--chat-id", "oc_1", "--text", "hi"]
    assert send_command("hi", user_id=OPEN_ID)[5:7] == ["--user-id", OPEN_ID]
    assert consume_command() == ["lark-cli", "event", "consume", "im.message.receive_v1", "--as", "bot"]
    for targets in ({}, {"chat_id": "oc_1", "user_id": OPEN_ID}):
        with pytest.raises(ValueError):
            send_command("hi", **targets)


def test_a_reply_goes_to_its_chat(tmp_path: Path, lark_cli: Path) -> None:
    adapter = _adapter(tmp_path)
    adapter._correlation_chat["c1"] = "oc_team"
    reply = MessageEnvelope.create(source=channel_address("lark"), target=channel_address("lark"),
                                   message_type=MessageType.AGENT_REPLY, payload={"text": "两个任务在跑。"},
                                   correlation_id="c1")
    receipt = asyncio.run(adapter.deliver(reply))
    assert receipt.success
    assert _calls(lark_cli) == [["im", "+messages-send", "--as", "bot", "--chat-id", "oc_team", "--text",
                                 "两个任务在跑。"]]


def test_a_failed_send_says_why(tmp_path: Path, lark_cli: Path) -> None:
    adapter = _adapter(tmp_path)
    adapter._correlation_chat["c1"] = "oc_gone"
    reply = MessageEnvelope.create(source=channel_address("lark"), target=channel_address("lark"),
                                   message_type=MessageType.AGENT_REPLY, payload={"text": "x"}, correlation_id="c1")
    receipt = asyncio.run(adapter.deliver(reply))
    assert (receipt.success, receipt.error) == (False, "lark-cli exited 2: Bot/User can NOT be out of the chat")
    notified = asyncio.run(adapter.notify("ou_gone", "x"))
    assert (notified.success, notified.error) == (False, "lark-cli exited 2: Bot/User can NOT be out of the chat")


def test_received_messages_reach_the_manager(tmp_path: Path, lark_cli: Path) -> None:
    async def main() -> Any:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = _adapter(tmp_path)
        adapter._bus = bus  # what start() sets
        adapter._running = True
        await asyncio.wait_for(adapter._run_event_consumer(), timeout=10)  # the stand-in prints one event and exits
        queue = bus.get_queue("manager")
        messages = [queue.get_nowait() for _ in range(queue.qsize())]
        await bus.shutdown()
        return messages, dict(adapter._correlation_chat)

    messages, conversations = asyncio.run(main())
    (message,) = messages
    assert message.payload["text"] == "status please"
    assert message.payload["channel_context"] == {"chat_id": "oc_team", "sender_id": OPEN_ID, "message_id": "om_9"}
    assert conversations == {message.correlation_id: "oc_team"}
    assert _calls(lark_cli) == [consume_command()[1:]]


def test_a_consumer_that_stops_is_started_again_after_a_pause(tmp_path: Path, lark_cli: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    pauses: list[float] = []
    sleep = asyncio.sleep

    async def main() -> None:
        adapter = _adapter(tmp_path)
        adapter._bus = MessageBus(runtime_root=tmp_path)
        adapter._running = True

        async def pause(seconds: float) -> None:
            pauses.append(seconds)
            if len(pauses) == 2:
                adapter._running = False
            await sleep(0)

        monkeypatch.setattr(asyncio, "sleep", pause)
        await asyncio.wait_for(adapter._event_collector_loop(), timeout=10)
        await adapter._bus.shutdown()

    asyncio.run(main())
    assert pauses == [10, 10] and len(_calls(lark_cli)) == 2  # not a tight loop of lark-cli processes
