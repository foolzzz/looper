"""Card buttons in the channels (assistant card actions RFC): a reply's card goes after its text with buttons that
carry only a token, and a press on the Lark bot's long connection (a local platform) or a Telegram callback query is
answered in time and the card updated when its action ends. Only a sender the bot admits may press."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from weakref import WeakSet

import pytest

from lark_platform_fake import FakeLarkPlatform
from looper.agents import card_actions
from looper.agents.bus import MessageBus
from looper.agents.channels import lark
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.lark_bot import make_adapter
from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address
from looper.agents.turn_context import SpeakerKind
from tests.db_support import DSN, run_db

ASKER = "ou_asker"
BLOCK = "需要你确认：\n删除项目 sample。\n回复「确认」就执行；回复别的就不执行。"
CARD = card_actions.Card(BLOCK, (card_actions.Button("确认", "tok:confirm", "primary"),
                                 card_actions.Button("取消", "tok:cancel")))


def _reply(channel_id: str, chat: str, cards: list[dict[str, Any]]) -> MessageEnvelope:
    channel = channel_id.removeprefix("channel-")
    return MessageEnvelope.create(source=manager_address("manager"), target=channel_address(channel, channel_id),
                                  message_type=MessageType.AGENT_REPLY, payload={"text": f"好的。\n\n{BLOCK}"},
                                  correlation_id="corr-1", metadata={"reply_route": {"chat_id": chat},
                                                                     "cards": cards})


def _switch(monkeypatch: pytest.MonkeyPatch, on: bool = True) -> None:
    """Card buttons on or off without a database (``cards.actions.enabled``)."""
    async def enabled(db: Any = None) -> bool:
        return on

    monkeypatch.setattr(card_actions, "enabled", enabled)


def _answered(press: Any, seconds: float = 0) -> Any:
    """``card_actions.press`` as a confirmation answers it: at once, with an action that ends after ``seconds``."""
    async def answer(event: card_actions.Press, **_: Any) -> card_actions.Answer:
        press.append(event)

        async def later() -> card_actions.Card:
            await asyncio.sleep(seconds)
            return CARD.settled("✅ 已执行。")

        return card_actions.Answer("已确认，正在执行", "success", card=CARD.settled("⏳ 已确认，正在执行…"), later=later())
    return answer


def test_lark_a_reply_sends_its_card_after_its_text_and_a_press_is_answered_then_updated(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lark, "observe_chat", AsyncMock())
    _switch(monkeypatch)
    presses: list[card_actions.Press] = []
    monkeypatch.setattr(card_actions, "press", _answered(presses, seconds=0.3))
    monkeypatch.setattr(card_actions, "ANSWER_SECONDS", 0.1)  # the action outlasts the answer

    async def main() -> None:
        async with FakeLarkPlatform() as fake:
            bus = MessageBus(runtime_root=tmp_path)
            adapter = make_adapter({"app_id": fake.app_id}, "test-secret", runtime_root=tmp_path,
                                   allowlist=SenderAllowlist("channel-lark_bot", frozenset({ASKER})),
                                   on_rejected=None, bot_open_id=fake.bot["open_id"], base_url=fake.base_url)
            try:
                await adapter.start(bus)
                assert (await adapter.deliver(_reply("channel-lark_bot", "oc_dm", [CARD.to_dict()]))).success
                text, card = fake.sent
                assert (text["msg_type"], text["content"]["zh_cn"]["content"][0][0]["text"]) == ("post", "好的。")
                assert card["msg_type"] == "interactive" and card["chat"] == "oc_dm"
                (markdown, row) = card["content"]["body"]["elements"]
                assert markdown == {"tag": "markdown", "content": BLOCK}
                assert [column["elements"][0]["behaviors"] for column in row["columns"]] == [
                    [{"type": "callback", "value": {"looper": "tok:confirm"}}],
                    [{"type": "callback", "value": {"looper": "tok:cancel"}}]]

                await fake.push_card_action("tok:confirm", operator="ou_stranger", frame_id="card-1")
                await fake.push_card_action("tok:confirm", operator=ASKER, message_id=card["message_id"],
                                            frame_id="card-2")
                await fake.wait_acks(2)
                answers = {frame: json.loads(base64.b64decode(ack["data"])) for frame, ack in fake.acks}
                assert answers["card-1"] == {"toast": {"type": "error", "content": "只有发起人能确认"}}
                assert answers["card-2"]["toast"] == {"type": "success", "content": "已确认，正在执行"}
                assert answers["card-2"]["card"]["data"]["body"]["elements"] == [
                    {"tag": "markdown", "content": f"{BLOCK}\n\n⏳ 已确认，正在执行…"}]
                assert [(p.channel_id, p.presser, p.value, p.chat, p.message_id) for p in presses] == [
                    ("channel-lark_bot", ASKER, "tok:confirm", "oc_chat", card["message_id"])]
                async with asyncio.timeout(2):
                    while not any(sent["kind"] == "update" for sent in fake.sent):
                        await asyncio.sleep(0.01)
                (update,) = [sent for sent in fake.sent if sent["kind"] == "update"]
                assert update["parent"] == card["message_id"]
                assert update["content"]["body"]["elements"][0]["content"].endswith("✅ 已执行。")
            finally:
                await adapter.stop()
                await bus.shutdown()

    asyncio.run(asyncio.wait_for(main(), 10))


def test_lark_cli_and_web_replies_keep_the_block_in_their_text() -> None:
    text, cards = card_actions.shown(f"好的。\n\n{BLOCK}", [])
    assert (text, cards) == (f"好的。\n\n{BLOCK}", [])
    text, cards = card_actions.shown("（已隐藏）", [CARD.to_dict()])  # a group check replaced the reply
    assert (text, cards) == ("（已隐藏）", [])


def _telegram(tmp_path: Path) -> tuple[TelegramChannelAdapter, Any]:
    adapter = TelegramChannelAdapter(bot_token="0:not-a-token",
                                     allowlist=SenderAllowlist("channel-telegram", frozenset({"7"})))
    adapter._bus = SimpleNamespace(runtime_root=tmp_path)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=lambda **kw: SimpleNamespace(message_id=55)),
                          edit_message_text=AsyncMock())
    adapter._app = SimpleNamespace(bot=bot)
    return adapter, bot


def test_telegram_a_reply_sends_its_card_with_an_inline_keyboard(tmp_path: Path) -> None:
    adapter, bot = _telegram(tmp_path)
    adapter._remember("corr-1", 99)
    assert asyncio.run(adapter.deliver(_reply("channel-telegram", "99", [CARD.to_dict()]))).success
    answer, card = bot.send_message.await_args_list
    assert answer.kwargs["text"] == "好的。" and "reply_markup" not in answer.kwargs
    assert card.kwargs["text"] == BLOCK
    assert [[(key.text, key.callback_data) for key in row] for row in card.kwargs["reply_markup"].inline_keyboard] == [
        [("确认", "tok:confirm"), ("取消", "tok:cancel")]]


@pytest.mark.parametrize("presser", [7, 8])
def test_telegram_a_press_is_answered_and_its_card_edited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         presser: int) -> None:
    _switch(monkeypatch)
    presses: list[card_actions.Press] = []
    monkeypatch.setattr(card_actions, "press", _answered(presses))
    adapter, bot = _telegram(tmp_path)
    query = SimpleNamespace(data="tok:confirm", answer=AsyncMock(), message=SimpleNamespace(message_id=55))
    update = SimpleNamespace(update_id=3, callback_query=query, effective_user=SimpleNamespace(id=presser),
                             effective_chat=SimpleNamespace(id=99))

    async def main() -> None:
        await adapter._on_button(update, None)
        await asyncio.gather(*card_actions._kept)

    asyncio.run(main())
    if presser == 8:
        assert presses == [] and query.answer.await_args.kwargs == {"text": "只有发起人能确认"}
        bot.edit_message_text.assert_not_awaited()
        return
    assert [(p.channel_id, p.presser, p.value, p.chat, p.message_id) for p in presses] == [
        ("channel-telegram", "7", "tok:confirm", "99", "55")]
    assert query.answer.await_args.kwargs == {"text": "已确认，正在执行"}
    # The action ended within the answer's time: the card is edited once, to what it came to.
    assert [(call.kwargs["text"], call.kwargs["reply_markup"]) for call in bot.edit_message_text.await_args_list] == [
        (f"{BLOCK}\n\n✅ 已执行。", None)]


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_lark_bot_a_confirmation_card_pressed_on_the_long_connection_runs_it_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole chain against a database and a local platform: the Manager asks, its reply's card goes out, the
    asker presses 确认 twice on the long connection; the call runs once and the card says so."""
    from looper.agents.ops import Turn
    from looper.agents.ops.service import ManagerOps, Platform
    from looper.agents.tiers.manager import _with_blocks
    from looper.db import projects, system_settings

    async def test(pool: Any) -> None:
        project = await projects.create_project(name="sample", work_dir="/srv/sample", pool=pool)
        await system_settings.put_settings({card_actions.ENABLED_KEY: "true"}, pool=pool)
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="test-token")
        monkeypatch.setitem(card_actions._handlers, "confirmation", ops.press)
        turn = Turn(channel="lark_bot", chat="oc_dm", message_id="om_ask", sender=ASKER, channel_id="channel-lark_bot",
                    speaker=SpeakerKind.OPERATOR)
        ops.begin_turn(turn)
        assert (await ops.call("looper_projects", {"action": "delete", "project": "sample"}))["status"] == \
            "needs_confirmation"
        ops.end_turn(turn)
        reply = _reply("channel-lark_bot", "oc_dm", turn.cards)
        reply = MessageEnvelope(**{**reply.__dict__, "payload": {"text": _with_blocks("好的。", turn.blocks)}})
        async with FakeLarkPlatform() as fake:
            bus = MessageBus(runtime_root=tmp_path)
            adapter = make_adapter({"app_id": fake.app_id}, "test-secret", runtime_root=tmp_path,
                                   allowlist=SenderAllowlist("channel-lark_bot", frozenset({ASKER})),
                                   on_rejected=None, bot_open_id=fake.bot["open_id"], base_url=fake.base_url)
            try:
                await adapter.start(bus)
                await adapter.deliver(reply)
                (card,) = [sent for sent in fake.sent if sent["msg_type"] == "interactive"]
                buttons = [column["elements"][0] for column in card["content"]["body"]["elements"][1]["columns"]]
                confirm = buttons[0]["behaviors"][0]["value"]["looper"]
                assert buttons[0]["text"]["content"] == "确认" and confirm.endswith(":confirm")
                await fake.push_card_action(confirm, operator=ASKER, chat_id="oc_dm", message_id="om_forwarded",
                                            event_id="ev-press-0", frame_id="press-0")  # a forwarded copy
                await fake.wait_acks(1, timeout=5)
                for number in (1, 2):
                    await fake.push_card_action(confirm, operator=ASKER, chat_id="oc_dm", message_id=card["message_id"],
                                                event_id=f"ev-press-{number}", frame_id=f"press-{number}")
                    await fake.wait_acks(number + 1, timeout=5)
                assert tuple(await pool.fetchrow("SELECT chat, message_id FROM card_actions")) == (
                    "oc_dm", card["message_id"])  # noted from the send's answer
                answers = {frame: json.loads(base64.b64decode(ack["data"])) for frame, ack in fake.acks}
                assert answers["press-0"]["toast"]["content"] == "这张卡片只能在发出它的会话里操作"
                first = answers["press-1"]
                assert first["toast"]["type"] == "success"
                assert first["card"]["data"]["body"]["elements"][0]["content"].endswith("✅ 已执行。")
                assert answers["press-2"]["toast"]["content"] == "已处理"
            finally:
                await adapter.stop()
                await bus.shutdown()
        assert await projects.get_project(project["id"], pool=pool) is None
        assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE status = 'ok'") == 1

    run_db(test, monkeypatch, timeout=30)


def test_telegram_a_press_is_left_unanswered_while_card_buttons_are_off(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, on=False)
    presses: list[card_actions.Press] = []
    monkeypatch.setattr(card_actions, "press", _answered(presses))
    adapter, bot = _telegram(tmp_path)
    query = SimpleNamespace(data="tok:confirm", answer=AsyncMock(), message=SimpleNamespace(message_id=55))
    update = SimpleNamespace(update_id=3, callback_query=query, effective_user=SimpleNamespace(id=7),
                             effective_chat=SimpleNamespace(id=99))
    asyncio.run(adapter._on_button(update, None))
    assert presses == [] and not query.answer.await_count and not bot.edit_message_text.await_count


@pytest.mark.parametrize(("policy", "senders", "admitted"), [
    ("listed", frozenset(), False), ("listed", frozenset({"ou_member"}), True), ("chat", frozenset(), True)])
def test_lark_a_press_is_admitted_as_a_message_in_that_chat_would_be(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str, senders: frozenset[str], admitted: bool) -> None:
    """A group bound with ``sender_policy`` ``chat`` admits its members' presses, as it admits their messages; the
    card's own allowed confirmers (``card_actions.press``) narrow it further."""
    from looper.agents.channels.chat_routing import RouteAction, RouteDecision, SenderPolicy

    _switch(monkeypatch)
    presses: list[card_actions.Press] = []
    monkeypatch.setattr(card_actions, "press", _answered(presses))
    adapter = make_adapter({"app_id": "cli_example"}, "test-secret", runtime_root=tmp_path,
                           allowlist=SenderAllowlist("channel-lark_bot", frozenset({ASKER})), on_rejected=None,
                           bot_open_id="ou_bot", base_url="http://127.0.0.1:9",
                           resolve_chat=lambda *_: RouteDecision(RouteAction.ADMIT, "manager", explicit=True,
                                                                 senders=senders, sender_policy=SenderPolicy(policy)))

    async def main() -> dict[str, Any] | None:
        try:
            return await adapter._card_action({"operator_id": "ou_member", "chat_id": "oc_team",
                                               "message_id": "", "value": "tok:confirm"})
        finally:
            await adapter._transport.aclose()

    answer = asyncio.run(main())
    assert [p.presser for p in presses] == (["ou_member"] if admitted else [])
    assert answer["toast"]["content"] == ("已确认，正在执行" if admitted else "只有发起人能确认")


class _Gateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._outbox_wakes: WeakSet[asyncio.Event] = WeakSet()

    async def write(self, kind: str, call: Any, *, keyed: bool = False) -> Any:
        self.calls.append((kind, keyed))
        return await call()

    async def edit(self, message_id: str, call: Any) -> Any:
        self.calls.append(("edit", message_id))
        return await call()


@pytest.mark.parametrize("gated", [False, True])
def test_lark_card_writes_go_through_the_outbound_gateway_while_it_is_on(
        gated: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = SimpleNamespace(send_card=AsyncMock(return_value="om_1"), reply_card=AsyncMock(return_value="om_2"),
                                update_card=AsyncMock())
    gateway = _Gateway()
    from looper.agents import chat_settings
    from looper.agents.channels import outbound

    monkeypatch.setattr(chat_settings, "_outbound_enabled", gated)
    monkeypatch.setattr(outbound, "Gateway", lambda *args, **kwargs: gateway)
    adapter = lark.LarkChannelAdapter(allowlist=SenderAllowlist("channel-lark_bot", frozenset({ASKER})),
                                      channel_type="lark_bot", transport=transport)
    cards = adapter._cards
    assert cards is not None

    async def main() -> None:
        assert await cards.send({"c": 1}, chat_id="oc_dm", idempotency_key="gate-1") == "om_1"
        assert await cards.reply("om_parent", {"c": 2}) == "om_2"
        await cards.update("om_1", {"c": 3})

    asyncio.run(main())
    assert gateway.calls == ([("send", True), ("reply", False), ("edit", "om_1")] if gated else [])
    transport.send_card.assert_awaited_once_with({"c": 1}, chat_id="oc_dm", user_id=None, idempotency_key="gate-1")
    transport.reply_card.assert_awaited_once_with("om_parent", {"c": 2}, idempotency_key=None, reply_in_thread=False)
    transport.update_card.assert_awaited_once_with("om_1", {"c": 3})


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_lark_bot_off_a_press_gets_the_acknowledgement_it_got_before_cards(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``cards.actions.enabled`` absent, a press of a Looper card is acknowledged exactly like a message event
    (``{"code":200}``, the frame's own headers and ``biz_rt``) and nothing is sent."""
    async def test(pool: Any) -> None:
        async with FakeLarkPlatform() as fake:
            bus = MessageBus(runtime_root=tmp_path)
            adapter = make_adapter({"app_id": fake.app_id}, "test-secret", runtime_root=tmp_path,
                                   allowlist=SenderAllowlist("channel-lark_bot", frozenset({ASKER})),
                                   on_rejected=None, bot_open_id=fake.bot["open_id"], base_url=fake.base_url)
            try:
                await adapter.start(bus)
                await fake.push_card_action("tok:confirm", operator=ASKER, frame_id="press-1")
                await fake.push_message(text="hello", sender="ou_unlisted", message_id="om_text")
                await fake.wait_acks(2, timeout=5)
            finally:
                await adapter.stop()
                await bus.shutdown()
        press, message = fake.ack_frames
        assert press.payload == message.payload == b'{"code":200}'
        assert [key for key, _ in press.headers] == ["type", "message_id", "trace_id", "sum", "seq", "biz_rt"]
        assert [key for key, _ in press.headers] == [key for key, _ in message.headers]
        assert fake.sent == [] and await pool.fetchval("SELECT count(*) FROM card_actions") == 0

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("case", ["noted", "unnoted", "slow", "error"])
def test_lark_a_card_sent_to_a_user_is_pressed_from_their_direct_chat_only(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    """A gate card goes to the user's open_id and its press names their direct chat. Noted from the send's answer,
    the card is that chat and message; not noted (the service stopped right after sending), the press is admitted
    from a chat the platform says is a direct one, by that user, and from no group. A lookup of the chat that takes
    3.2 s, or fails, refuses the press within the checks' time and nothing is claimed, then or later."""
    import time
    from datetime import timedelta

    from looper.agents.task_notice import NotifyTarget, TargetKind
    from looper.db import card_actions as db_cards, system_settings

    async def test(pool: Any) -> None:
        await system_settings.put_settings({card_actions.ENABLED_KEY: "true"}, pool=pool)
        card = card_actions.Card("测试关卡", (card_actions.Button("按现在的结论结束", "tok:end"),))
        await db_cards.create(token="tok", kind="gate", subject="step", digest="d", payload={"card": card.to_dict()},
                              manager_id=None, channel_id="channel-lark_bot", chat=ASKER, allowed=[ASKER],
                              ttl=timedelta(days=1), pool=pool)
        pressed: list[card_actions.Press] = []

        async def gate(row: dict[str, Any], event: card_actions.Press, choice: str) -> card_actions.Answer:
            pressed.append(event)
            return card_actions.Answer("已交给规划者", "success")

        monkeypatch.setitem(card_actions._handlers, "gate", gate)
        direct = f"oc_p2p_{ASKER}"
        async with FakeLarkPlatform() as fake:
            adapter = make_adapter({"app_id": fake.app_id}, "test-secret", runtime_root=tmp_path,
                                   allowlist=SenderAllowlist("channel-lark_bot", frozenset({ASKER})),
                                   on_rejected=None, bot_open_id=fake.bot["open_id"], base_url=fake.base_url)
            try:
                message = "om_unnoted"
                if case == "noted":
                    receipt = await adapter.post_card(NotifyTarget(TargetKind.USER, ASKER), card)
                    (message,) = receipt.external_ids
                    assert tuple(await pool.fetchrow("SELECT chat, message_id FROM card_actions")) == (direct, message)
                elif case == "slow":
                    fake.slow_once[f"/open-apis/im/v1/chats/{direct}"] = 3.2
                elif case == "error":
                    async def failing(chat_id: str) -> str:
                        raise TimeoutError("the chat lookup timed out")

                    monkeypatch.setattr(adapter._transport, "chat_mode", failing)

                async def press(chat: str) -> str:
                    answer = await adapter._card_action({"operator_id": ASKER, "chat_id": chat, "message_id": message,
                                                         "value": "tok:end"})
                    return answer["toast"]["content"]

                if case in ("slow", "error"):
                    began = time.monotonic()
                    assert await press(direct) == "没能及时核对这次点击，没有处理，请再点一次"
                    assert time.monotonic() - began < card_actions.AUTHORIZE_SECONDS + 0.5
                    await asyncio.sleep(3.5 if case == "slow" else 0.2)  # the lookup's own end changes nothing
                    assert pressed == [] and await pool.fetchval("SELECT handled_at FROM card_actions") is None
                    return
                assert await press("oc_team") == "这张卡片只能在发出它的会话里操作"
                assert pressed == [] and await pool.fetchval("SELECT handled_at FROM card_actions") is None
                assert await press(direct) == "已交给规划者"
                assert [event.chat for event in pressed] == [direct]
            finally:
                await adapter._transport.aclose()

    run_db(test, monkeypatch, timeout=30)


def test_telegram_a_card_is_bound_to_the_chat_and_message_the_send_answered(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.task_notice import NotifyTarget, TargetKind

    bound: list[tuple[str, str | None, str | None]] = []

    async def bind(card: card_actions.Card, message_id: str | None, chat: str | None = None) -> None:
        bound.append((card.token, message_id, chat))

    monkeypatch.setattr(card_actions, "bind", bind)
    adapter, bot = _telegram(tmp_path)
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=55, chat_id=4243))
    assert asyncio.run(adapter.post_card(NotifyTarget(TargetKind.CHAT, "99"), CARD)).external_ids == ("55",)
    assert bound == [("tok", "55", "4243")]


def test_lark_edit_card_keeps_the_interactive_format_and_removes_buttons(tmp_path: Path) -> None:
    from looper.agents.task_notice import NotifyTarget, TargetKind

    async def main() -> None:
        async with FakeLarkPlatform() as fake:
            adapter = make_adapter({"app_id": fake.app_id}, "test-secret", runtime_root=tmp_path,
                                   allowlist=SenderAllowlist("channel-lark_bot", frozenset({ASKER})),
                                   on_rejected=None, bot_open_id=fake.bot["open_id"], base_url=fake.base_url)
            try:
                target = NotifyTarget(TargetKind.CHAT, "oc_dm")
                posted = await adapter.post_card(target, CARD)
                assert (await adapter.edit_card(target, posted.external_ids[0], card_actions.Card("已处理"))).success
                sent, edited = fake.sent
                assert sent["msg_type"] == "interactive" and edited["kind"] == "update"
                assert edited["parent"] == posted.external_ids[0]
                assert edited["content"] == card_actions.lark_card(card_actions.Card("已处理"))
            finally:
                await adapter._transport.aclose()

    asyncio.run(main())


def test_telegram_edit_card_explicitly_clears_the_inline_keyboard(tmp_path: Path) -> None:
    from looper.agents.task_notice import NotifyTarget, TargetKind

    adapter, bot = _telegram(tmp_path)
    assert asyncio.run(adapter.edit_card(NotifyTarget(TargetKind.CHAT, "99"), "55",
                                         card_actions.Card("已处理"))).success
    bot.edit_message_text.assert_awaited_once_with(chat_id=99, message_id=55, text="已处理", reply_markup=None)
