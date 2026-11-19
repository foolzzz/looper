"""A task's end notice in a group chat: the whole conclusion in ordered messages, and a link only when the dashboard
is public (``task_notice.notice_text``, ``split_message``, the channels' ``deliver``). A direct chat keeps the lead
and the link. Nothing reaches a real chat: Lark runs on the stand-in lark-cli, Telegram on a stand-in bot."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lark_cli_stand_in import OPEN_ID
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.links import DashboardUrl, dashboard_url
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.task_notice import (
    GROUP_MESSAGE_CHARS, Notice, NotifyChannel, NotifyTarget, TargetKind, lead, notice_text, split_message,
)

PUBLIC = DashboardUrl("https://looper.example.com", public=True)
LOCAL = DashboardUrl("http://127.0.0.1:8767")
SECTIONS = "## Delivered\n" + "result line\n" * 60 + "\n## Verification\npytest: 3 passed\n\n## Open issues\nnone"


def test_the_dashboard_address_says_whether_it_is_public() -> None:
    assert dashboard_url(host="0.0.0.0", port=8767) == "http://127.0.0.1:8767"
    assert not dashboard_url(host="0.0.0.0", port=8767).public
    public = dashboard_url(host="0.0.0.0", port=8767, public_url="https://looper.example.com/")
    assert public == "https://looper.example.com" and public.public


@pytest.mark.parametrize(("group", "base", "whole", "link"), [
    (True, PUBLIC, True, "https://looper.example.com/#tasks/t1"),
    (True, LOCAL, True, None),
    (True, "", True, None),  # a plain string is never public
    (False, PUBLIC, False, "https://looper.example.com/#tasks/t1"),
    (False, LOCAL, False, "http://127.0.0.1:8767/#tasks/t1"),
])
@pytest.mark.parametrize("channel", [NotifyChannel.LARK, NotifyChannel.TELEGRAM])
def test_a_group_notice_has_the_whole_summary_and_a_link_only_when_public(
    channel: NotifyChannel, group: bool, base: str, whole: bool, link: str | None,
) -> None:
    text = notice_text("t1", "calc", "completed", SECTIONS, channel=channel, dashboard_url=base, group=group)
    lines = text.split("\n")
    assert lines[0] == "✅ 任务完成 · calc"
    assert ("pytest: 3 passed" in text and text.count("result line") == 60) is whole
    if not whole:  # a direct chat: the first section, clipped
        assert lead(SECTIONS) in text and text.count("result line") < 60 and "Verification" not in text
    if link is None:
        assert "#tasks" not in text
    else:
        assert lines[-1] == (f"[{link}]({link})" if channel.is_lark else link)


def test_a_group_notice_shows_the_delivery_summary_once() -> None:
    delivery = "## Process record\nRoles: worker\nReviews: passed"
    summary = "## Delivered\nthe answer\n\n" + delivery
    text = notice_text("t1", "calc", "completed", summary, channel=NotifyChannel.LARK, dashboard_url=PUBLIC,
                       delivery_summary=delivery, group=True, extra="branch: x")
    assert text.count("## Process record") == 1
    assert text.split("\n")[1:-1] == ["## Delivered", "the answer", *delivery.split("\n"), "branch: x"]


def test_a_group_failure_note_is_whole_too() -> None:
    note = "## Why it ended\nA step failed.\n\n## Done\n- s1 (failed)"
    text = notice_text("t1", "calc", "failed", note, channel=NotifyChannel.LARK, group=True)
    assert text == f"❌ 任务失败 · calc\n{note}"


@pytest.mark.parametrize("limit", [100, 400, GROUP_MESSAGE_CHARS])
def test_split_message_keeps_every_line_in_order_within_the_limit(limit: int) -> None:
    text = "\n".join(f"line {n} " + "x" * (n % 37) for n in range(400))
    parts = split_message(text, limit)
    assert all(len(part) <= limit for part in parts) and len(parts) > 1 or len(text) <= limit
    assert "\n".join(parts) == text if len(parts) == 1 else "\n".join(parts).split("\n") == text.split("\n")


def test_a_short_message_is_one_part() -> None:
    assert split_message("short", 100) == ["short"]


def test_a_single_overlong_line_is_cut_where_it_must_be() -> None:
    parts = split_message("head\n" + "y" * 250, 100)
    assert all(len(part) <= 100 for part in parts) and "".join(parts).replace("head\n", "", 1).count("y") == 250


def test_a_cut_inside_a_code_fence_closes_and_reopens_it() -> None:
    body = "\n".join(f"code {n}" for n in range(60))
    text = f"intro\n```python\n{body}\n```\nafter"
    parts = split_message(text, 120)
    assert len(parts) > 2 and all(len(part) <= 120 for part in parts)
    for part in parts:  # every part has balanced fences
        assert sum(line.startswith("```") for line in part.split("\n")) % 2 == 0
    assert parts[1].startswith("```python\n") and parts[0].endswith("\n```")
    assert [line for part in parts for line in part.split("\n") if line.startswith("code ")] == body.split("\n")


def test_a_fence_that_fits_is_not_cut() -> None:
    fence = "```\n" + "\n".join(f"c{n}" for n in range(10)) + "\n```"
    parts = split_message("a" * 80 + "\n" + fence, 120)
    assert any(part == fence or part.endswith(fence) or fence in part for part in parts)
    assert all(sum(line.startswith("```") for line in part.split("\n")) % 2 == 0 for part in parts)


# --- The channels ---

LONG = "✅ 任务完成 · calc\n" + "\n".join(f"- finding number {n}: " + "detail " * 20 for n in range(80))
LARK_GROUP = NotifyTarget(TargetKind.CHAT, "oc_team", mention=OPEN_ID, group=True)
LARK_USER = NotifyTarget(TargetKind.USER, OPEN_ID)


def _lark(tmp_path: Path) -> Any:
    from looper.agents.channels.lark import LarkChannelAdapter

    return LarkChannelAdapter(profile="other", runtime_root=tmp_path,
                              allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))


def _deliver(channel: str, notice: Notice) -> MessageEnvelope:
    return MessageEnvelope.create(source=MANAGER_ADDRESS, target=channel_address(channel),
                                  message_type=MessageType.CHAT_NOTICE, payload=notice.payload(),
                                  correlation_id="never-seen")


def test_lark_sends_a_long_group_notice_in_order_the_first_mentioning_the_requester(
    tmp_path: Path, lark_cli_stand_in: Path,
) -> None:
    receipt = asyncio.run(_lark(tmp_path).deliver(_deliver(
        "lark", Notice(NotifyChannel.LARK, LARK_GROUP, LONG, channel_id="channel-lark"))))
    assert receipt.success
    sends = [json.loads(line) for line in lark_cli_stand_in.read_text().splitlines()]
    texts = [argv[-1] for argv in sends]
    assert len(sends) > 1 and all(argv[6:8] == ["--chat-id", "oc_team"] or argv[4:6] == ["--chat-id", "oc_team"]
                                  for argv in sends)
    assert texts[0].startswith(f'<at user_id="{OPEN_ID}"></at> ✅ 任务完成 · calc\n')
    assert not any("<at " in text for text in texts[1:])
    assert all(len(text) <= GROUP_MESSAGE_CHARS + 60 for text in texts)
    assert "\n".join([texts[0].split("</at> ", 1)[1], *texts[1:]]) == LONG


def test_lark_stops_a_group_notice_at_the_part_that_fails(tmp_path: Path, lark_cli_stand_in: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.channels.lark_cli import LarkCliError

    lark = _lark(tmp_path)
    sent: list[str] = []

    async def send(chat_id: str | None = None, **kwargs: Any) -> None:
        if len(sent) == 1:
            raise LarkCliError("boom")
        sent.append(kwargs["markdown"])

    monkeypatch.setattr(lark._transport, "send", send)
    receipt = asyncio.run(lark.deliver(_deliver("lark", Notice(NotifyChannel.LARK, LARK_GROUP, LONG,
                                                              channel_id="channel-lark"))))
    assert not receipt.success and len(sent) == 1  # the first part is not sent again, nothing follows the failure


def test_lark_sends_a_long_direct_notice_as_one_message(tmp_path: Path, lark_cli_stand_in: Path) -> None:
    receipt = asyncio.run(_lark(tmp_path).deliver(_deliver(
        "lark", Notice(NotifyChannel.LARK, LARK_USER, LONG, channel_id="channel-lark"))))
    assert receipt.success
    (argv,) = [json.loads(line) for line in lark_cli_stand_in.read_text().splitlines()]
    assert argv[-1] == LONG


class _Bot:
    def __init__(self, fail_on: int | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail_on = fail_on

    async def send_message(self, **kwargs: Any) -> Any:
        if self.fail_on is not None and len(self.sent) + 1 == self.fail_on:
            raise RuntimeError("boom")
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=100 + len(self.sent))


def _telegram(bot: _Bot) -> Any:
    pytest.importorskip("telegram")
    from looper.agents.channels.telegram import TelegramChannelAdapter

    adapter = TelegramChannelAdapter(bot_token="0:x", allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))
    adapter._app = SimpleNamespace(bot=bot)
    return adapter


def test_telegram_sends_a_long_group_notice_in_order() -> None:
    bot = _Bot()
    group = NotifyTarget(TargetKind.CHAT, "-1001234", group=True)
    receipt = asyncio.run(_telegram(bot).deliver(_deliver(
        "telegram", Notice(NotifyChannel.TELEGRAM, group, LONG * 3, channel_id="channel-telegram"))))
    assert receipt.success and len(bot.sent) > 2
    assert all(sent["chat_id"] == -1001234 and len(sent["text"]) <= GROUP_MESSAGE_CHARS for sent in bot.sent)
    assert "\n".join(sent["text"] for sent in bot.sent) == LONG * 3


def test_telegram_stops_a_group_notice_at_the_part_that_fails() -> None:
    bot = _Bot(fail_on=2)
    group = NotifyTarget(TargetKind.CHAT, "-1001234", group=True)
    receipt = asyncio.run(_telegram(bot).deliver(_deliver(
        "telegram", Notice(NotifyChannel.TELEGRAM, group, LONG * 3, channel_id="channel-telegram"))))
    # Each part is tried as MarkdownV2 then as plain text; the second part fails both ways.
    assert not receipt.success and len(bot.sent) == 1 and bot.sent[0]["text"].startswith("✅ 任务完成")


def test_telegram_sends_a_direct_notice_as_before() -> None:
    bot = _Bot()
    receipt = asyncio.run(_telegram(bot).deliver(_deliver(
        "telegram", Notice(NotifyChannel.TELEGRAM, NotifyTarget(TargetKind.CHAT, "7"), "✅ 任务完成 · x",
                           channel_id="channel-telegram"))))
    assert receipt.success and [sent["text"] for sent in bot.sent] == ["✅ 任务完成 · x"]
