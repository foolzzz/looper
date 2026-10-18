"""A Telegram or Lark answer is stored once its channel has tried to send it (``chat_history.record_reply``, from
each adapter's outbound loop): as sent, or as not delivered with a typed reason (``DeliveryFailure``). Only an
answer the Manager names a record for is stored, and a storage failure neither stops the channel nor changes what
it sent. The bot is a stand-in (``tests.test_channel_post._Bot``) and so is ``lark-cli``; nothing reaches a real
chat."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from lark_cli_stand_in import OPEN_ID
from looper.agents.bus import MessageBus
from looper.agents.channels import chat_history
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.base import DeliveryFailure, DeliveryReceipt
from looper.agents.channels.chat_history import CHAT_RECORD, record_reply
from looper.agents.channels.lark import LarkChannelAdapter
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from tests.test_channel_post import _Bot, _telegram

TELEGRAM = channel_address("telegram", "channel-telegram")
LARK = channel_address("lark", "channel-lark")
RECORD = {"session_id": "telegram:12345", "message_id": "m1:reply"}


def _answer(target: Any, text: str = "ok", *, corr: str = "c1", record: dict[str, str] | None = RECORD
            ) -> MessageEnvelope:
    return MessageEnvelope.create(source=MANAGER_ADDRESS, target=target, message_type=MessageType.AGENT_REPLY,
                                  payload={"text": text}, correlation_id=corr,
                                  metadata={"origin_channel_id": target.agent_id,
                                            **({CHAT_RECORD: record} if record else {})})


class _Saved:
    """``db.chat.save_message``: keeps what it is asked to store; fails the first ``fail`` calls."""

    def __init__(self, fail: int = 0) -> None:
        self.rows: list[dict[str, Any]] = []
        self.fail = fail

    async def __call__(self, **kwargs: Any) -> None:
        if self.fail:
            self.fail -= 1
            raise OSError("the database is gone")
        self.rows.append(kwargs)


@pytest.mark.parametrize(("receipt", "stored"), [
    (DeliveryReceipt(channel="telegram", success=True, external_id="101"), (None, None)),
    (DeliveryReceipt(channel="telegram", success=False, error="Forbidden: bot was blocked by the user"),
     (DeliveryFailure.SEND_FAILED, "Forbidden: bot was blocked by the user")),
    (DeliveryReceipt(channel="telegram", success=False, error="no chat_id for correlation c1",
                     failure=DeliveryFailure.NO_CHAT), (DeliveryFailure.NO_CHAT, "no chat_id for correlation c1")),
    (DeliveryReceipt(channel="telegram", success=False), (DeliveryFailure.SEND_FAILED, None)),
])
def test_an_answer_is_stored_as_its_channel_delivered_it(monkeypatch: pytest.MonkeyPatch, receipt: DeliveryReceipt,
                                                         stored: tuple[Any, Any]) -> None:
    saved = _Saved()
    monkeypatch.setattr(chat_history.db_chat, "save_message", saved)
    asyncio.run(record_reply("telegram", _answer(TELEGRAM), receipt))
    assert saved.rows == [{"session_id": "telegram:12345", "message_id": "m1:reply", "role": "assistant",
                           "content": "ok", "channel": "telegram", "delivery_failure": stored[0],
                           "delivery_error": stored[1]}]


@pytest.mark.parametrize("envelope", [
    _answer(TELEGRAM, record=None),  # one the Manager does not keep (a user gate's, a Telegram group's)
    _answer(TELEGRAM, ""),  # nothing to show
    _answer(TELEGRAM, record={"session_id": "telegram:12345"}),  # not a record
])
def test_only_an_answer_the_manager_names_a_record_for_is_stored(monkeypatch: pytest.MonkeyPatch,
                                                                 envelope: MessageEnvelope) -> None:
    saved = _Saved()
    monkeypatch.setattr(chat_history.db_chat, "save_message", saved)
    asyncio.run(record_reply("telegram", envelope, DeliveryReceipt(channel="telegram", success=True)))
    assert saved.rows == []


def test_a_storage_failure_is_logged_and_never_raised(monkeypatch: pytest.MonkeyPatch,
                                                      caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(chat_history.db_chat, "save_message", _Saved(fail=1))
    with caplog.at_level("WARNING"):
        asyncio.run(record_reply("telegram", _answer(TELEGRAM), DeliveryReceipt(channel="telegram", success=True)))
    assert "could not store a telegram answer in telegram:12345" in caplog.text


def _through_the_loop(adapter: Any, envelopes: list[MessageEnvelope], saved: _Saved, count: int) -> None:
    """Run ``adapter``'s outbound loop on a bus until ``count`` answers were offered to storage (stored or not)."""

    async def go() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bus = MessageBus(runtime_root=Path(tmp))
            adapter._bus, adapter._running = bus, True
            offered = 0
            real = saved.__call__

            async def counting(**kwargs: Any) -> None:
                nonlocal offered
                offered += 1
                await real(**kwargs)

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(chat_history.db_chat, "save_message", counting)
                loop = asyncio.create_task(adapter._outbound_loop())
                for envelope in envelopes:
                    await bus.publish(envelope)
                for _ in range(200):
                    if offered >= count:
                        break
                    await asyncio.sleep(0.01)
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)
            await bus.shutdown()

    asyncio.run(go())


def test_telegram_stores_an_answer_after_sending_it_and_a_failed_one_as_not_delivered() -> None:
    bot = _Bot(fail_at=1)  # the second answer cannot be sent
    adapter = _telegram(bot)
    adapter._remember("c1", 12345)
    adapter._remember("c2", 12345)
    saved = _Saved()
    _through_the_loop(adapter, [_answer(TELEGRAM, "first"), _answer(
        TELEGRAM, "second", corr="c2", record={"session_id": "telegram:12345", "message_id": "m2:reply"})], saved, 2)
    assert [m["text"] for m in bot.sent] == ["first"]
    assert [(r["message_id"], r["content"], r["delivery_failure"]) for r in saved.rows] == [
        ("m1:reply", "first", None), ("m2:reply", "second", DeliveryFailure.SEND_FAILED)]
    # The channel's reason, without the bot's token.
    assert "AAE-secret_token" not in saved.rows[1]["delivery_error"] and "ConnectError" in saved.rows[1][
        "delivery_error"]


def test_telegram_stores_an_answer_to_a_chat_it_no_longer_knows_as_not_delivered() -> None:
    bot = _Bot()
    saved = _Saved()
    _through_the_loop(_telegram(bot), [_answer(TELEGRAM, corr="c-before-a-restart")], saved, 1)
    assert bot.sent == []
    assert [(r["delivery_failure"], r["delivery_error"]) for r in saved.rows] == [
        (DeliveryFailure.NO_CHAT, "no chat_id for correlation c-before-a-restart")]


def test_a_storage_failure_does_not_stop_the_channel_or_change_what_it_sent() -> None:
    bot = _Bot()
    adapter = _telegram(bot)
    adapter._remember("c1", 12345)
    adapter._remember("c2", 12345)
    saved = _Saved(fail=1)
    _through_the_loop(adapter, [_answer(TELEGRAM, "first"), _answer(
        TELEGRAM, "second", corr="c2", record={"session_id": "telegram:12345", "message_id": "m2:reply"})], saved, 2)
    assert [m["text"] for m in bot.sent] == ["first", "second"]
    assert [r["content"] for r in saved.rows] == ["second"]


def test_lark_stores_an_answer_after_sending_it_and_a_refused_or_lost_one_as_not_delivered(
    tmp_path: Path, lark_cli_stand_in: Path,
) -> None:
    adapter = LarkChannelAdapter(runtime_root=tmp_path, allowlist=SenderAllowlist("channel-lark",
                                                                                   frozenset({OPEN_ID})))
    adapter._correlation_chat["c1"] = "oc_dm"
    adapter._correlation_chat["c2"] = "oc_gone"  # the stand-in refuses to send there
    saved = _Saved()
    _through_the_loop(adapter, [
        _answer(LARK, "first", record={"session_id": "lark:oc_dm", "message_id": "m1:reply"}),
        _answer(LARK, "second", corr="c2", record={"session_id": "lark:oc_gone", "message_id": "m2:reply"}),
        _answer(LARK, "third", corr="c-before-a-restart", record={"session_id": "lark:oc_dm",
                                                                    "message_id": "m3:reply"})],
        saved, 3)
    assert [(r["session_id"], r["channel"], r["delivery_failure"]) for r in saved.rows] == [
        ("lark:oc_dm", "lark", None), ("lark:oc_gone", "lark", DeliveryFailure.SEND_FAILED),
        ("lark:oc_dm", "lark", DeliveryFailure.NO_CHAT)]
    assert "lark-cli exited" in saved.rows[1]["delivery_error"]
