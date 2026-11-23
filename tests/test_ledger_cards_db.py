"""An operator confirms the ledger's relayed decisions through the same records as typed writes."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import card_actions
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address
from looper.agents.ops import Turn
from looper.agents.ops.service import ManagerOps, Platform
from looper.db import ledger, system_settings
from tests.db_support import run_db


async def _setup(pool, *, on=True, channel="telegram", operator="7"):
    await system_settings.put_settings({"ledger.enabled": "true", card_actions.ENABLED_KEY: str(on).lower()}, pool=pool)
    await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, display_name, config) "
                       "VALUES ($1,$2,'Synthetic bot',$3::jsonb)", f"channel-{channel}", channel,
                       json.dumps({"operator_sender_id": operator}))
    return await ledger.apply("add", ledger.Actor("relayed", "api", relayed_by="client:demo",
                                                  channel_id=f"channel-{channel}", chat="99"),
                              new=ledger.NewItem("decision", "Use PostgreSQL", detail="For the demo"),
                              quote="Use PostgreSQL", db=pool)


async def _read(*, action="get", channel="telegram", sender="7", group=False):
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:1")
    turn = Turn(channel=channel, channel_id=f"channel-{channel}", chat="99", sender=sender, group=group,
                message_id="m-ask", envelope=SimpleNamespace(payload={"text": "Show the decision"}))
    ops.begin_turn(turn)
    answer = await ops.call("looper_ledger_update", {"action": action, **({"item": "L-1"} if action == "get" else {})})
    assert answer["status"] == "ok", answer
    return turn, answer


def test_ledger_cards_off_keep_the_reply_and_writes_unchanged(monkeypatch):
    async def check(pool):
        await _setup(pool, on=False)
        turn, answer = await _read()
        assert turn.blocks == [] and turn.cards == []
        assert answer["result"]["item"]["assurance"] == "relayed"
        assert await pool.fetchval("SELECT count(*) FROM card_actions") == 0
        assert await pool.fetchval("SELECT count(*) FROM ledger_events") == 1

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["get", "list"])
def test_operator_reads_offer_confirmation_only_for_relayed_decisions(monkeypatch, action):
    async def check(pool):
        await _setup(pool)
        turn, _ = await _read(action=action)
        assert len(turn.cards) == 1
        card = card_actions.Card.from_dict(turn.cards[0])
        assert card.text in turn.blocks
        assert [(button.label, button.value.partition(":")[2]) for button in card.buttons] == [("确认", "confirm")]
        row = await pool.fetchrow("SELECT kind,subject,allowed FROM card_actions")
        assert (row["kind"], row["subject"], row["allowed"]) == ("ledger", "L-0001", ["7"])
        for fields in ({"sender": "8"}, {"group": True}):
            other, _ = await _read(action=action, **fields)
            assert other.cards == [] and other.blocks == []
        await ledger.apply("confirm", ledger.Actor("user", "dashboard"), item_id=1, db=pool)
        confirmed, _ = await _read(action=action)
        assert confirmed.cards == [] and confirmed.blocks == []

    run_db(check, monkeypatch, timeout=30)


def test_telegram_ledger_card_delivers_and_confirms_the_original_item_once(tmp_path, monkeypatch):
    async def check(pool):
        from looper.agents import ledger_cards

        await _setup(pool)
        ledger_cards.install()
        turn, _ = await _read()
        card = card_actions.Card.from_dict(turn.cards[0])
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=55)),
                              edit_message_text=AsyncMock())
        adapter = TelegramChannelAdapter(bot_token="0:not-a-token",
            allowlist=SenderAllowlist("channel-telegram", frozenset({"7", "8"})))
        adapter._app = SimpleNamespace(bot=bot)
        adapter._bus = SimpleNamespace(runtime_root=tmp_path)
        reply = MessageEnvelope.create(source=manager_address("manager"), target=channel_address("telegram"),
            message_type=MessageType.AGENT_REPLY, payload={"text": "\n\n".join(turn.blocks)}, correlation_id="c",
            metadata={"reply_route": {"chat_id": "99"}, "cards": turn.cards})
        assert (await adapter.deliver(reply)).success
        sent = bot.send_message.await_args.kwargs
        assert sent["text"] == card.text
        assert [(key.text, key.callback_data) for row in sent["reply_markup"].inline_keyboard for key in row] == [
            ("确认", card.buttons[0].value)]
        assert (await pool.fetchrow("SELECT message_id,chat FROM card_actions"))["message_id"] == "55"
        await ledger.apply("note", ledger.Actor("relayed", "api", relayed_by="client:demo"), item_id=1,
                           note="A note leaves the decision unchanged")
        for sender, chat, message in ((8, 99, 55), (7, 100, 55), (7, 99, 56)):
            query = SimpleNamespace(data=card.buttons[0].value, answer=AsyncMock(),
                                    message=SimpleNamespace(message_id=message))
            await adapter._on_button(SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=sender),
                                                     effective_chat=SimpleNamespace(id=chat)), None)
            assert (await ledger.get_item(1, db=pool))["assurance"] == "relayed"
            assert await pool.fetchval("SELECT handled_at FROM card_actions") is None
        query = SimpleNamespace(data=card.buttons[0].value, answer=AsyncMock(), message=SimpleNamespace(message_id=55))
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7),
                                 effective_chat=SimpleNamespace(id=99))
        await adapter._on_button(update, None)
        await adapter._on_button(update, None)
        assert (await ledger.get_item(1, db=pool))["assurance"] == "user"
        events = await ledger.list_events(1, db=pool)
        assert [event["action"] for event in events] == ["add", "note", "confirm"]
        assert (events[-1]["actor_kind"], events[-1]["via"], events[-1]["actor_id"], events[-1]["message_id"]) == (
            "user", "telegram", "telegram:7", "55")
        row = await pool.fetchrow("SELECT handled_via,outcome FROM card_actions")
        assert (row["handled_via"], row["outcome"]) == ("button", "ok")
        assert bot.edit_message_text.await_args.kwargs["reply_markup"] is None

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("mutation", ["confirm", "amend", "cancel"])
def test_typed_ledger_changes_settle_the_same_cards(monkeypatch, mutation):
    async def check(pool):
        from looper.agents import ledger_cards

        await _setup(pool)
        ledger_cards.install()
        turn, _ = await _read()
        card = card_actions.Card.from_dict(turn.cards[0])
        await card_actions.bind(card, "55")
        await ledger.apply(mutation, ledger.Actor("user", "cli", session_label="synthetic session"), item_id=1,
                           fields={"title": "Use SQLite"} if mutation == "amend" else None, db=pool)
        row = await pool.fetchrow("SELECT handled_via,outcome FROM card_actions")
        assert (row["handled_via"], row["outcome"]) == ("typed", "ok" if mutation == "confirm" else "stale")
        answer = await card_actions.press(card_actions.Press("channel-telegram", "7", card.buttons[0].value,
                                                            chat="99", message_id="55"))
        assert answer.toast == card_actions.HANDLED
        assert [event["action"] for event in await ledger.list_events(1, db=pool)] == ["add", mutation]

    run_db(check, monkeypatch, timeout=30)


def test_operator_identity_changes_and_slow_checks_never_claim_a_ledger_card(monkeypatch):
    async def check(pool):
        from looper.agents import ledger_cards, operator_identity

        await _setup(pool)
        ledger_cards.install()
        turn, _ = await _read()
        card = card_actions.Card.from_dict(turn.cards[0])
        event = card_actions.Press("channel-telegram", "7", card.buttons[0].value, chat="99")
        await pool.execute("UPDATE channel_configs SET config='{}'::jsonb")
        answer = await card_actions.press(event)
        assert answer.level == "error"
        assert await pool.fetchval("SELECT handled_at FROM card_actions") is None

        async def slow(_):
            await asyncio.sleep(1)
            return "7"

        monkeypatch.setattr(operator_identity, "operator_sender_id", slow)
        monkeypatch.setattr(card_actions, "AUTHORIZE_SECONDS", .02)
        answer = await card_actions.press(event)
        assert answer.toast == card_actions.UNCHECKED
        assert await pool.fetchval("SELECT handled_at FROM card_actions") is None
        assert (await ledger.get_item(1, db=pool))["assurance"] == "relayed"

    run_db(check, monkeypatch, timeout=30)


def test_button_and_typed_confirmation_race_adds_only_one_event(monkeypatch):
    async def check(pool):
        from looper.agents import ledger_cards

        await _setup(pool)
        ledger_cards.install()
        turn, _ = await _read()
        card = card_actions.Card.from_dict(turn.cards[0])
        event = card_actions.Press("channel-telegram", "7", card.buttons[0].value, chat="99")
        results = await asyncio.gather(card_actions.press(event),
            ledger.apply("confirm", ledger.Actor("user", "dashboard"), item_id=1), return_exceptions=True)
        assert not any(isinstance(result, Exception) and not isinstance(result, ledger.LedgerRefused)
                       for result in results), results
        assert (await ledger.get_item(1))["assurance"] == "user"
        assert [entry["action"] for entry in await ledger.list_events(1)] == ["add", "confirm"]
        assert await pool.fetchval("SELECT outcome FROM card_actions") == "ok"

    run_db(check, monkeypatch, timeout=30)
