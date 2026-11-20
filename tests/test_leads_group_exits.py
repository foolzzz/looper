"""Every way text reaches a group chat goes through the leads backstop (message-leads-intents RFC, R-2): a task's end
notice (also one the bus replays, or another agent sends) at the channel that sends it, a user gate's post, and a
task result the Manager sends; a direct chat's are left alone."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lark_cli_stand_in import OPEN_ID
from looper.agents.bus import MessageBus
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.base import PostReceipt
from looper.agents.message import MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.ops import leads as ops_leads
from looper.agents.task_notice import Notice, NotifyChannel, NotifyTarget, TargetKind

SUMMARY = "Project Aurora launch moves to December 3 after the security review"
QUOTED = f"✅ 任务完成 · 发布\n结论：{SUMMARY}。"
LARK_GROUP = NotifyTarget(TargetKind.CHAT, "oc_team", mention=OPEN_ID, group=True)
LARK_USER = NotifyTarget(TargetKind.USER, OPEN_ID)


@pytest.fixture(autouse=True)
def _searched():
    ops_leads._SEEN.clear()
    ops_leads._WITHHELD.clear()
    ops_leads.remember("manager", [("lead-1", SUMMARY)])
    yield
    ops_leads._SEEN.clear()
    ops_leads._WITHHELD.clear()


def _notice_envelope(channel: NotifyChannel, target: NotifyTarget, channel_id: str) -> MessageEnvelope:
    notice = Notice(channel, target, QUOTED, channel_id=channel_id)
    # The source a replay or another agent's notice has is not the Manager's turn: the channel guards it all the same.
    return MessageEnvelope.create(source=TASK_ROUTER_ADDRESS, target=channel_address(channel.value),
                                  message_type=MessageType.CHAT_NOTICE, payload=notice.payload(),
                                  correlation_id="never-seen")


@pytest.mark.parametrize("target, withheld", [(LARK_GROUP, True), (LARK_USER, False)])
def test_lark_sends_no_group_notice_that_quotes_a_searched_lead(tmp_path: Path, lark_cli_stand_in: Path,
                                                               target: NotifyTarget, withheld: bool) -> None:
    from looper.agents.channels.lark import LarkChannelAdapter

    lark = LarkChannelAdapter(profile="other", runtime_root=tmp_path,
                              allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID})))
    receipt = asyncio.run(lark.deliver(_notice_envelope(NotifyChannel.LARK, target, "channel-lark")))
    assert receipt.success
    sent = " ".join(" ".join(json.loads(line)) for line in lark_cli_stand_in.read_text().splitlines())
    assert (ops_leads.WITHHELD_REPLY in sent) is withheld and ("Project Aurora" in sent) is not withheld


@pytest.mark.parametrize("group, withheld", [(True, True), (False, False)])
def test_telegram_sends_no_group_notice_that_quotes_a_searched_lead(group: bool, withheld: bool) -> None:
    pytest.importorskip("telegram")
    from looper.agents.channels.telegram import TelegramChannelAdapter

    sent: list[str] = []

    async def send_message(**kwargs: Any) -> Any:
        sent.append(kwargs["text"])
        return SimpleNamespace(message_id=100 + len(sent))

    telegram = TelegramChannelAdapter(bot_token="0:x", allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))
    telegram._app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
    target = NotifyTarget(TargetKind.CHAT, "-1000001" if group else "1000001", group=group)
    receipt = asyncio.run(telegram.deliver(_notice_envelope(NotifyChannel.TELEGRAM, target, "channel-telegram")))
    assert receipt.success
    text = "\n".join(sent)
    assert ("Aurora" not in text) is withheld


@pytest.mark.parametrize("group, withheld", [(True, True), (False, False)])
def test_a_gate_post_to_a_group_that_quotes_a_searched_lead_is_not_sent(group: bool, withheld: bool) -> None:
    from looper.agents.channels.registry import ChannelRegistry

    posted: list[str] = []

    async def post(target: Any, text: str, **kwargs: Any) -> PostReceipt:
        posted.append(text)
        return PostReceipt(True, external_ids=("om_1",))

    registry = ChannelRegistry(bus=None)
    registry._channels["channel-lark"] = SimpleNamespace(post=post)
    target = LARK_GROUP if group else LARK_USER
    asyncio.run(registry.post("channel-lark", target, f"规划者的问题：{SUMMARY}？"))
    assert (posted == [ops_leads.WITHHELD_REPLY]) is withheld


@pytest.mark.parametrize("group, withheld", [(True, True), (False, False)])
def test_the_managers_task_result_to_a_group_that_quotes_a_searched_lead_is_not_sent(
    tmp_path: Path, group: bool, withheld: bool,
) -> None:
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

    async def main() -> list[str]:
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        await manager._handle_task_result(MessageEnvelope.create(
            source=TASK_ROUTER_ADDRESS, target=MANAGER_ADDRESS, message_type=MessageType.WORK_RESULT,
            payload={"summary": QUOTED}, correlation_id="c1",
            metadata={"origin_channel_id": "channel-lark", **({"origin_group": True} if group else {})}))
        queue = bus.get_queue("channel-lark")
        texts = [queue.get_nowait().payload["text"] for _ in range(queue.qsize())]
        await bus.shutdown()
        return texts

    assert (asyncio.run(main()) == [ops_leads.WITHHELD_REPLY]) is withheld
