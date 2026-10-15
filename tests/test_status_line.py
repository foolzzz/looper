"""One status message per message answered in Lark and Telegram, edited in place: the status line itself, then
each channel end to end (Lark through the stand-in lark-cli, Telegram through a stand-in bot); and the reaction
each channel adds to a message it hands to the Manager. Nothing reaches a real chat or user."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lark_cli_stand_in import OPEN_ID, OTHER
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.status_line import MAX_PROGRESS_EDITS, StatusLines, phase_label
from looper.agents.bus import MessageBus
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
    def __init__(self, *, fail_reaction: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_reaction = fail_reaction

    async def send_message(self, **kwargs: Any) -> Any:
        self.calls.append(("send", kwargs))
        return SimpleNamespace(message_id=100 + len(self.calls))

    async def edit_message_text(self, **kwargs: Any) -> Any:
        self.calls.append(("edit", kwargs))
        return SimpleNamespace(message_id=kwargs["message_id"])

    async def set_message_reaction(self, **kwargs: Any) -> bool:
        self.calls.append(("react", kwargs))
        if self.fail_reaction:
            raise RuntimeError("Bad Request: reactions are unavailable in this chat")
        return True


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


# --- The reaction to a message handed to the Manager ---


def _lark_dm(message_id: str, sender: str = OPEN_ID, **fields: Any) -> dict[str, Any]:
    return {"type": "im.message.receive_v1", "chat_id": "oc_dm", "chat_type": "p2p", "message_id": message_id,
            "message_type": "text", "sender_id": sender, "sender_type": "user", "content": "build it", **fields}


async def _settled(adapter: Any) -> None:
    for _ in range(500):
        if not adapter._reactions._pending:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("a reaction call did not finish")


def _reactions(log: Path) -> list[list[str]]:
    return [c for c in _calls(log) if c[c.index("im"):c.index("im") + 3] == ["im", "reactions", "create"]]


def _manager_texts(bus: MessageBus) -> list[str]:
    queue = bus.get_queue("manager")
    return [queue.get_nowait().payload["text"] for _ in range(queue.qsize())]


def test_lark_reacts_get_to_an_admitted_direct_message_without_holding_it(tmp_path: Path,
                                                                          lark_cli_stand_in: Path) -> None:
    from looper.agents.channels.lark import LarkChannelAdapter

    async def main() -> tuple[list[str], int]:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = LarkChannelAdapter(profile="other", runtime_root=tmp_path,
                                     allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))
        adapter._bus = bus
        await adapter._handle_lark_event(_lark_dm("om_9"))
        # The Manager has the message while the reaction call is still running.
        texts, pending = _manager_texts(bus), len(adapter._reactions._pending)
        await _settled(adapter)
        await bus.shutdown()
        return texts, pending

    texts, pending = asyncio.run(main())
    assert texts == ["build it"] and pending == 1
    assert _reactions(lark_cli_stand_in) == [["--profile", "other", "im", "reactions", "create", "--as", "bot",
                                              "--message-id", "om_9", "--data",
                                              '{"reaction_type": {"emoji_type": "Get"}}']]


def test_lark_a_rejected_sender_or_a_group_message_gets_no_reaction(tmp_path: Path,
                                                                    lark_cli_stand_in: Path) -> None:
    from looper.agents.channels.lark import LarkChannelAdapter

    async def main() -> list[str]:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = LarkChannelAdapter(runtime_root=tmp_path,
                                     allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))
        adapter._bus = bus
        await adapter._handle_lark_event(_lark_dm("om_1", sender=OTHER))
        await adapter._handle_lark_event(_lark_dm("om_2", chat_type="group"))
        await adapter._handle_lark_event(_lark_dm("om_3", message_type="image", content="[Image]"))
        await _settled(adapter)
        texts = _manager_texts(bus)
        await bus.shutdown()
        return texts

    assert asyncio.run(main()) == []
    assert _reactions(lark_cli_stand_in) == []


def test_lark_a_failed_reaction_is_logged_once_per_run_and_the_message_still_goes_on(
    tmp_path: Path, lark_cli_stand_in: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    from looper.agents.channels.lark import LarkChannelAdapter

    async def main() -> list[str]:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = LarkChannelAdapter(runtime_root=tmp_path,
                                     allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))
        adapter._bus = bus
        # The stand-in refuses a reaction to a message id ending in "noscope", naming the scope the app lacks.
        for n in (1, 2):
            await adapter._handle_lark_event(_lark_dm(f"om_{n}noscope"))
            await _settled(adapter)
        await adapter.stop()  # a new run of the channel logs its first failure again
        await adapter._handle_lark_event(_lark_dm("om_3noscope"))
        await _settled(adapter)
        texts = _manager_texts(bus)
        await bus.shutdown()
        return texts

    with caplog.at_level(logging.WARNING):
        texts = asyncio.run(main())
    assert texts == ["build it"] * 3
    assert len(_reactions(lark_cli_stand_in)) == 3  # one call per message, none retried
    lines = [r.getMessage() for r in caplog.records if "could not react" in r.getMessage()]
    assert len(lines) == 2
    assert lines[0].startswith("channel-lark: could not react to a message")
    assert "missing scope: im:message.reactions:write_only" in lines[0]
    assert not [c for c in _calls(lark_cli_stand_in) if "+messages-send" in c]  # never a chat message


def test_the_reaction_command_dry_run_shape() -> None:
    from looper.agents.channels.lark_cli import reaction_command

    assert reaction_command("om_1", profile="p") == [
        "lark-cli", "--profile", "p", "im", "reactions", "create", "--as", "bot", "--message-id", "om_1",
        "--data", '{"reaction_type": {"emoji_type": "Get"}}']


@pytest.mark.parametrize(("error", "reason"), [
    ({"message": "access denied", "missing_scopes": ["im:message.reactions:write_only"]},
     "access denied (missing scope: im:message.reactions:write_only)"),
    ({"message": "app has not applied for the required scope(s): im:message.reactions:write_only",
      "missing_scopes": ["im:message.reactions:write_only"]},
     "app has not applied for the required scope(s): im:message.reactions:write_only"),
    ({"message": "permission denied", "required_scope": "im:message"}, "permission denied (missing scope: im:message)"),
    ({"message": "not logged in"}, "not logged in"),
])
def test_a_failure_names_the_scope_lark_cli_reports_missing(error: dict[str, Any], reason: str) -> None:
    from looper.agents.channels.lark_cli import failure

    assert failure(json.dumps({"ok": False, "error": error})) == reason
    assert failure("panic: something broke") == "panic: something broke"


def _tg_message(user_id: int, chat_type: str = "private") -> Any:
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id, username="someone"),
                           effective_chat=SimpleNamespace(id=7, type=chat_type),
                           message=SimpleNamespace(message_id=55, text="build it"))


def test_telegram_reacts_with_eyes_to_an_admitted_direct_message(caplog: pytest.LogCaptureFixture,
                                                                 tmp_path: Path) -> None:
    pytest.importorskip("telegram")
    from looper.agents.channels.telegram import TelegramChannelAdapter

    async def main(bot: _Bot) -> list[str]:
        bus = MessageBus(runtime_root=tmp_path)
        adapter = TelegramChannelAdapter(bot_token="0:not-a-token",
                                         allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))
        adapter._app = SimpleNamespace(bot=bot)
        adapter._bus = bus
        await adapter._on_message(_tg_message(7), None)
        await adapter._on_message(_tg_message(8), None)  # not on the list
        await adapter._on_message(_tg_message(7, chat_type="group"), None)  # answered, not acknowledged
        await adapter._cmd_status(_tg_message(7), None)
        await adapter._on_message(_tg_message(7), None)
        await _settled(adapter)
        texts = _manager_texts(bus)
        await bus.shutdown()
        return texts

    bot = _Bot()
    assert asyncio.run(main(bot)) == ["build it", "build it", "当前任务进度怎么样？", "build it"]
    assert bot.calls == [("react", {"chat_id": 7, "message_id": 55, "reaction": "👀"})] * 3

    failing = _Bot(fail_reaction=True)
    with caplog.at_level(logging.WARNING):
        assert len(asyncio.run(main(failing))) == 4
    assert len(failing.calls) == 3  # one call per message, none retried, nothing sent
    (line,) = [r.getMessage() for r in caplog.records if "could not react" in r.getMessage()]
    assert line.startswith("channel-telegram: could not react to a message") and "unavailable" in line
