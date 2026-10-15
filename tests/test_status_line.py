"""One status message per message answered in Lark and Telegram, edited in place: the status line itself, then
each channel end to end (Lark through the stand-in lark-cli, Telegram through a stand-in bot). Nothing reaches a
real chat or user."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lark_cli_stand_in import OPEN_ID
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.status_line import MAX_PROGRESS_EDITS, StatusLines, phase_label
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address

TOOLS = ["Bash", "Read", "Edit", "Grep", "WebFetch"]


def _phase(n: int) -> dict[str, Any]:
    if n % 3 == 0:
        return {"text": "thinking", "delta": False, "phase": True, "activity": "thinking"}
    tool = TOOLS[n % len(TOOLS)]
    return {"text": f"正在使用工具: {tool}", "delta": False, "phase": True, "activity": "tool", "tool": tool}


class _Chat:
    """A chat that records what the status line sends and edits."""

    def __init__(self, *, fail_send: bool = False, fail_edit: bool = False) -> None:
        self.sent: list[str] = []
        self.edits: list[str] = []
        self.fail_send = fail_send
        self.fail_edit = fail_edit

    async def send(self, chat: str, text: str) -> str | None:
        if self.fail_send:
            raise RuntimeError("Bot/User can NOT be out of the chat")
        self.sent.append(text)
        return f"om_{len(self.sent)}"

    async def edit(self, chat: str, message_id: str, text: str) -> None:
        if self.fail_edit:
            raise RuntimeError("the message was recalled")
        self.edits.append(text)


def _lines(chat: _Chat, **kwargs: Any) -> StatusLines:
    return StatusLines(channel="channel-test", send=chat.send, edit=chat.edit, **kwargs)


@pytest.mark.parametrize(("payload", "label"), [
    ({"phase": True, "activity": "thinking", "text": "thinking"}, ("思考中", False)),
    ({"phase": True, "activity": "tool", "tool": "Bash"}, ("运行命令 (Bash)", True)),
    ({"phase": True, "activity": "tool", "tool": "Read"}, ("读文件 (Read)", True)),
    ({"phase": True, "activity": "tool", "tool": "MultiEdit"}, ("改文件 (MultiEdit)", True)),
    ({"phase": True, "activity": "tool", "tool": "mcp__argos__query"}, ("调用工具 (mcp__argos__query)", True)),
    ({"phase": True, "activity": "tool", "tool": "Whatever"}, ("使用工具 (Whatever)", True)),
    ({"phase": True, "text": "整理结果"}, ("整理结果", False)),
    ({"text": "chunk", "delta": True}, None),
    ({"text": "not a phase"}, None),
])
def test_phase_labels(payload: dict[str, Any], label: tuple[str, bool] | None) -> None:
    assert phase_label(payload) == label


def test_a_burst_of_phases_is_one_message_then_the_closing_edit() -> None:
    chat = _Chat()

    async def main() -> None:
        lines = _lines(chat, interval=5.0)
        for n in range(10):
            await lines.update("c1", "oc_1", _phase(n))
        await lines.finish("c1")

    asyncio.run(main())
    assert len(chat.sent) == 1 and chat.sent[0].startswith("⏳ 处理中：思考中 · ")
    # All ten came within the edit interval: no progress edit, only the closing one.
    assert len(chat.edits) == 1 and chat.edits[0].startswith("✅ 完成 · 用时 0s · 工具 6 次")


def test_edits_are_throttled_and_show_the_latest_phase() -> None:
    chat = _Chat()

    async def main() -> None:
        lines = _lines(chat, interval=0.1)
        for n in range(1, 12):
            await lines.update("c1", "oc_1", _phase(n))
            await asyncio.sleep(0.03)
        await asyncio.sleep(0.15)  # the trailing edit shows the last phase
        await lines.finish("c1")

    asyncio.run(main())
    progress = chat.edits[:-1]
    assert len(chat.sent) == 1
    assert 2 <= len(progress) <= 11 * 0.03 / 0.1 + 2  # about one per interval, never one per phase
    assert progress[-1].startswith("⏳ 处理中：读文件 (Read)")  # the last phase, shown by the trailing edit
    assert chat.edits[-1].startswith("✅ 完成")


def test_the_edit_cap_stops_progress_but_not_the_closing_edit() -> None:
    chat = _Chat()

    async def main() -> None:
        lines = _lines(chat, interval=0.01, max_edits=4)
        for n in range(1, 30):
            await lines.update("c1", "oc_1", _phase(n))
            await asyncio.sleep(0.015)
        await lines.finish("c1", error="claude exited with status 1\nTraceback ...")

    asyncio.run(main())
    assert len(chat.sent) == 1 and len(chat.edits) == 4 + 1
    assert chat.edits[-1] == "❌ 出错：claude exited with status 1 Traceback ..."
    assert MAX_PROGRESS_EDITS + 1 <= 20  # Feishu's limit per message


def test_an_edit_failure_is_logged_once_and_never_becomes_more_messages(caplog: pytest.LogCaptureFixture) -> None:
    chat = _Chat(fail_edit=True)

    async def main() -> None:
        lines = _lines(chat, interval=0.01)
        for n in range(1, 8):
            await lines.update("c1", "oc_1", _phase(n))
            await asyncio.sleep(0.015)
        await lines.finish("c1")

    with caplog.at_level(logging.WARNING):
        asyncio.run(main())
    assert len(chat.sent) == 1 and chat.edits == []
    assert caplog.text.count("could not edit the status message") == 1


def test_a_status_message_that_could_not_be_sent_is_not_sent_again(caplog: pytest.LogCaptureFixture) -> None:
    chat = _Chat(fail_send=True)

    async def main() -> None:
        lines = _lines(chat, interval=0.01)
        for n in range(5):
            await lines.update("c1", "oc_1", _phase(n))
            await asyncio.sleep(0.015)
        await lines.finish("c1")

    with caplog.at_level(logging.WARNING):
        asyncio.run(main())
    assert (chat.sent, chat.edits) == ([], [])
    assert caplog.text.count("could not send the status message") == 1


def test_a_phase_after_the_answer_is_ignored_and_state_is_bounded() -> None:
    chat = _Chat()

    async def main() -> StatusLines:
        lines = _lines(chat)
        await lines.update("c1", "oc_1", _phase(0))
        await lines.finish("c1")
        await lines.update("c1", "oc_1", _phase(1))  # late: no new status message
        for n in range(250):
            await lines.update(f"c{n + 2}", "oc_1", _phase(0))
        return lines

    lines = asyncio.run(main())
    assert len(chat.sent) == 1 + 250
    assert lines.tracked() == 200


# --- Lark, end to end through the stand-in lark-cli ---


def _envelope(kind: MessageType, payload: dict[str, Any], correlation_id: str = "c1") -> MessageEnvelope:
    return MessageEnvelope.create(source=MANAGER_ADDRESS, target=channel_address("lark"), message_type=kind,
                                  payload=payload, correlation_id=correlation_id)


def _calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_lark_sends_one_status_message_the_answer_then_closes_it(tmp_path: Path, lark_cli_stand_in: Path) -> None:
    from looper.agents.channels.lark import LarkChannelAdapter

    async def main() -> None:
        adapter = LarkChannelAdapter(profile="other", runtime_root=tmp_path,
                                     allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))
        adapter._correlation_chat["c1"] = "oc_dm"
        for n in range(10):
            await adapter.deliver(_envelope(MessageType.STATUS_UPDATE, _phase(n)))
            await adapter.deliver(_envelope(MessageType.STATUS_UPDATE, {"text": "tok", "delta": True}))
        assert (await adapter.deliver(_envelope(MessageType.AGENT_REPLY, {"text": "| a | b |\n|---|---|"}))).success
        # The Manager speaking on its own later: a message, no status.
        await adapter.deliver(_envelope(MessageType.AGENT_REPLY, {"text": "后来想到的", "autonomous": True}))

    asyncio.run(main())
    calls = _calls(lark_cli_stand_in)
    sends = [c for c in calls if "+messages-send" in c]
    edits = [c for c in calls if "+messages-edit" in c]
    assert [c[c.index("--chat-id") + 2] for c in sends] == ["--text", "--markdown", "--markdown"]
    assert sends[0][-1].startswith("⏳ 处理中：思考中") and sends[1][-1] == "| a | b |\n|---|---|"
    assert sends[2][-1] == "后来想到的"
    # The closing edit, after the answer, on the status message the first send returned.
    assert calls.index(edits[-1]) > calls.index(sends[1])
    assert edits[-1] == ["--profile", "other", "im", "+messages-edit", "--as", "bot", "--message-id", "om_0001",
                         "--text", edits[-1][-1]]
    assert edits[-1][-1].startswith("✅ 完成 · 用时 ") and edits[-1][-1].endswith("工具 6 次")


def test_lark_a_failed_turn_closes_the_status_with_the_reason(tmp_path: Path, lark_cli_stand_in: Path) -> None:
    from looper.agents.channels.lark import LarkChannelAdapter

    async def main() -> None:
        adapter = LarkChannelAdapter(runtime_root=tmp_path,
                                     allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))
        adapter._correlation_chat["c1"] = "oc_dm"
        await adapter.deliver(_envelope(MessageType.STATUS_UPDATE, _phase(0)))
        await adapter.deliver(_envelope(MessageType.AGENT_REPLY, {
            "text": "Manager 出错，没能完成这次回复：claude exited 1", "error": "claude exited 1"}))

    asyncio.run(main())
    edits = [c for c in _calls(lark_cli_stand_in) if "+messages-edit" in c]
    assert [c[-1] for c in edits] == ["❌ 出错：claude exited 1"]


def test_lark_an_edit_failure_sends_nothing_more(tmp_path: Path, lark_cli_stand_in: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.channels.lark import LarkChannelAdapter

    # The status message's edits fail: the stand-in refuses edits of a message id ending in "gone".
    monkeypatch.setattr("looper.agents.channels.lark.sent_message_id", lambda answer: "om_gone")

    async def main() -> None:
        adapter = LarkChannelAdapter(runtime_root=tmp_path,
                                     allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))
        adapter._status._interval = 0.01
        adapter._correlation_chat["c1"] = "oc_dm"
        for n in range(6):
            await adapter.deliver(_envelope(MessageType.STATUS_UPDATE, _phase(n)))
            await asyncio.sleep(0.02)
        await adapter.deliver(_envelope(MessageType.AGENT_REPLY, {"text": "done"}))

    asyncio.run(main())
    calls = _calls(lark_cli_stand_in)
    assert len([c for c in calls if "+messages-send" in c]) == 2  # the status message and the answer
    assert len([c for c in calls if "+messages-edit" in c]) == 1  # the one that failed


def test_the_edit_command_dry_run_shape() -> None:
    from looper.agents.channels.lark_cli import edit_command

    assert edit_command("om_1", text="✅ 完成", profile="p") == [
        "lark-cli", "--profile", "p", "im", "+messages-edit", "--as", "bot", "--message-id", "om_1", "--text", "✅ 完成"]
    with pytest.raises(ValueError):
        edit_command("om_1")


# --- Telegram, with a stand-in bot ---


class _Bot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send_message(self, **kwargs: Any) -> Any:
        self.calls.append(("send", kwargs))
        return SimpleNamespace(message_id=100 + len(self.calls))

    async def edit_message_text(self, **kwargs: Any) -> Any:
        self.calls.append(("edit", kwargs))
        return SimpleNamespace(message_id=kwargs["message_id"])


def test_telegram_has_the_same_status_line() -> None:
    pytest.importorskip("telegram")
    from looper.agents.channels.telegram import TelegramChannelAdapter

    bot = _Bot()

    async def main() -> None:
        adapter = TelegramChannelAdapter(bot_token="0:not-a-token",
                                         allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))
        adapter._app = SimpleNamespace(bot=bot)
        adapter._correlation_chat["c1"] = 7
        for n in range(10):
            await adapter.deliver(_envelope(MessageType.STATUS_UPDATE, _phase(n)))
        await adapter.deliver(_envelope(MessageType.AGENT_REPLY, {"text": "answer"}))
        await adapter.deliver(_envelope(MessageType.AGENT_REPLY, {"text": "on its own", "autonomous": True}))

    asyncio.run(main())
    kinds = [kind for kind, _ in bot.calls]
    assert kinds == ["send", "send", "edit", "send"]  # status, answer, closing edit, the autonomous reply
    status, answer, closing, own = (kwargs for _, kwargs in bot.calls)
    assert status["text"].startswith("⏳ 处理中：思考中") and "parse_mode" not in status
    assert answer["text"] == "answer"
    assert (closing["chat_id"], closing["message_id"]) == (7, 101) and closing["text"].startswith("✅ 完成")
    assert own["text"] == "on its own"
