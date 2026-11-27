"""Card buttons (assistant card actions RFC) against a disposable database: a Manager's confirmation and a user gate
answered with one press, once, by the person they ask; off, nothing is stored and the model sees the same."""

from __future__ import annotations

import asyncio
import functools
import json
from typing import Any

import pytest

from looper.agents import card_actions
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.ops import Turn
from looper.agents.turn_context import SpeakerKind
from looper.agents.ops.service import ManagerOps, Platform, _Pending
from tests.db_support import DSN, dropped_tables, run_db
from tests.test_gate_channels_db import _Bus, _Poster, _say, _task_with_gate

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

BOT = "channel-lark_bot"
ASKER, OTHER = "ou_asker", "ou_other"
DELETE = {"action": "delete", "project": "sample"}


async def _switch(pool: Any, on: bool = True) -> None:
    from looper.db import system_settings

    await system_settings.put_settings({card_actions.ENABLED_KEY: "true" if on else "false"}, pool=pool)


async def _sample(pool: Any) -> dict[str, Any]:
    from looper.db import projects

    return await projects.create_project(name="sample", work_dir="/srv/sample", pool=pool)


async def _exists(pool: Any, project: dict[str, Any]) -> bool:
    from looper.db import projects

    return await projects.get_project(project["id"], pool=pool) is not None


def _ops() -> ManagerOps:
    return ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="test-token")


def _turn(message_id: str, sender: str = ASKER, chat: str = "oc_dm") -> Turn:
    return Turn(channel="lark_bot", chat=chat, message_id=message_id, sender=sender, channel_id=BOT, speaker=SpeakerKind.OPERATOR)


async def _ask(ops: ManagerOps) -> tuple[dict[str, Any], Turn]:
    """The Manager asks to delete the project: a high-risk call, which waits for the user."""
    turn = _turn("om_ask")
    ops.begin_turn(turn)
    answer = await ops.call("looper_projects", DELETE)
    ops.end_turn(turn)
    return answer, turn


def _value(turn: Turn, choice: str = "confirm") -> str:
    (card,) = turn.cards
    return next(button["value"] for button in card["buttons"] if button["value"].endswith(f":{choice}"))


def _presser(monkeypatch: pytest.MonkeyPatch, ops: ManagerOps) -> Any:
    monkeypatch.setitem(card_actions._handlers, "confirmation", ops.press)

    async def press(value: str, presser: str = ASKER) -> card_actions.Answer:
        return await card_actions.press(card_actions.Press(BOT, presser, value, chat="oc_dm"))

    return press


async def _card_row(pool: Any, subject: str | None = None) -> tuple[Any, ...]:
    row = await pool.fetchrow("SELECT handled_by, handled_via, choice, outcome FROM card_actions "
                              "WHERE $1::text IS NULL OR subject = $1", subject)
    return tuple(row)


def test_off_nothing_is_stored_and_what_the_model_sees_does_not_change_with_the_switch(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.tiers.manager import _with_blocks, reply_envelope

    async def test(pool: Any) -> None:
        await _sample(pool)
        seen = []
        for on in (False, True):
            await _switch(pool, on)
            ops = _ops()
            answer, turn = await _ask(ops)
            following = _turn(f"om_next_{on}")
            note = ops.begin_turn(following)
            ops.end_turn(following)
            cid = answer["confirmation_id"]
            seen.append((json.dumps(answer, ensure_ascii=False).replace(cid, "ID"), note.replace(cid, "ID"),
                         turn.blocks))
            original = MessageEnvelope.create(source=channel_address("lark_bot", BOT), target=MANAGER_ADDRESS,
                                              message_type=MessageType.USER_MESSAGE, payload={"text": "删掉 sample"})
            reply = reply_envelope(original, _with_blocks("好的", turn.blocks), MANAGER_ADDRESS, cards=turn.cards)
            if not on:
                assert turn.cards == [] and "cards" not in reply.metadata
                assert await pool.fetchval("SELECT count(*) FROM card_actions") == 0
            else:
                assert reply.metadata["cards"] == turn.cards and len(turn.cards) == 1
        assert seen[0] == seen[1]

    run_db(test, monkeypatch, timeout=30)


def test_a_press_runs_the_confirmation_once_only_for_the_asker_and_a_typed_one_after_it_does_not(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _sample(pool)
        await _switch(pool)
        ops = _ops()
        press = _presser(monkeypatch, ops)
        asked, turn = await _ask(ops)
        (card,) = turn.cards
        assert card["text"] == turn.blocks[0] and [b["label"] for b in card["buttons"]] == ["确认", "取消"]
        assert {b["value"].partition(":")[2] for b in card["buttons"]} == {"confirm", "cancel"}  # a token, a key
        refused = await press(_value(turn), OTHER)
        assert (refused.toast, refused.level, refused.later) == ("只有发起人能确认", "error", None)
        answer = await press(_value(turn))
        assert (answer.toast, answer.card.buttons) == ("已确认，正在执行", ())
        done = await answer.later
        assert done.text.startswith(card["text"]) and "✅ 已执行。" in done.text and done.buttons == ()
        assert not await _exists(pool, project)
        assert (await press(_value(turn))).toast == "已处理"
        typed = _turn("om_next")
        assert ops.begin_turn(typed) == ""
        again = await ops.call("looper_confirm", {"confirmation_id": asked["confirmation_id"]})
        assert again["error"]["code"] == "conflict"
        actions = await pool.fetch("SELECT tool, status, confirmation_id FROM manager_actions ORDER BY id")
        assert [(a["tool"], a["status"]) for a in actions] == [
            ("looper_projects", "needs_confirmation"), ("looper_projects", "ok"), ("looper_confirm", "error")]
        assert actions[1]["confirmation_id"] == asked["confirmation_id"]
        assert await _card_row(pool) == (ASKER, "button", "confirm", "ok")

    run_db(test, monkeypatch, timeout=30)


def test_a_typed_confirmation_answers_its_card_too(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _sample(pool)
        await _switch(pool)
        ops = _ops()
        press = _presser(monkeypatch, ops)
        asked, turn = await _ask(ops)
        following = _turn("om_next")
        assert asked["confirmation_id"] in ops.begin_turn(following)
        assert (await ops.call("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))["status"] == "ok"
        ops.end_turn(following)
        assert not await _exists(pool, project)
        assert (await press(_value(turn))).toast == "已处理"
        assert await _card_row(pool) == (ASKER, "typed", "confirm", "ok")

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("how", ["cancel", "lapse", "expired", "off"])
def test_a_cancelled_lapsed_or_expired_card_or_buttons_turned_off_run_nothing(
        monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    async def test(pool: Any) -> None:
        project = await _sample(pool)
        await _switch(pool)
        ops = _ops()
        press = _presser(monkeypatch, ops)
        _, turn = await _ask(ops)
        if how == "cancel":
            assert (await press(_value(turn, "cancel"))).toast == "已取消"
        elif how == "lapse":  # the user's next message did not confirm it
            following = _turn("om_next")
            ops.begin_turn(following)
            ops.end_turn(following)
            await asyncio.gather(*ops._lapsing)
        elif how == "expired":
            await pool.execute("UPDATE card_actions SET expires_at = now() - interval '1 second'")
        else:
            await _switch(pool, False)
        answer = await press(_value(turn))
        assert answer.toast == {"off": "按钮已关闭，请打字回复"}.get(how, "已处理") and answer.later is None
        assert await _exists(pool, project)
        assert (await _card_row(pool))[3] == {"cancel": "cancelled", "lapse": "lapsed"}.get(how)

    run_db(test, monkeypatch, timeout=30)


def test_a_restart_brings_a_waiting_confirmation_back_but_never_one_with_a_secret(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _sample(pool)
        await _switch(pool)
        original = _ops()
        asked, _ = await _ask(original)
        (waiting,) = original.waiting_confirmations()
        assert waiting["impact"]
        secret_ops, secret_turn = _ops(), _turn("om_secret", chat="oc_other")
        pending = _Pending(id="c-secret", tool=secret_ops.scope.tools["looper_projects"],
                           arguments={**DELETE, "api_key": "sk-hidden"}, block="需要你确认：…", asked_in="om_secret",
                           at=0.0, confirmation={})
        secret_ops._pending[secret_turn.chat_key] = pending
        await secret_ops._card(pending, secret_turn)
        assert "sk-hidden" not in await pool.fetchval("SELECT payload::text FROM card_actions WHERE subject = 'c-secret'")

        restarted = _ops()
        assert await restarted.restore_confirmations() == 1
        (restored,) = restarted.waiting_confirmations()
        assert restored["impact"] == waiting["impact"]
        assert {key: restored[key] for key in ("confirmation_id", "channel_id", "chat")} == {
            key: waiting[key] for key in ("confirmation_id", "channel_id", "chat")}
        assert (await _card_row(pool, "c-secret"))[1:] == ("lapsed", None, "lapsed")
        following = _turn("om_next")
        assert asked["confirmation_id"] in restarted.begin_turn(following)
        assert (await restarted.call("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))["status"] == "ok"
        assert not await _exists(pool, project)

    run_db(test, monkeypatch, timeout=30)


LARK_USER = "ou_requester"


@pytest.mark.parametrize(("on", "origin"), [(False, BOT), (True, "channel-lark")])
def test_a_gate_message_posts_as_text_while_off_or_in_a_channel_without_buttons(
        monkeypatch: pytest.MonkeyPatch, on: bool, origin: str) -> None:
    from looper.agents.gate_courier import GateCourier

    async def test(pool: Any) -> None:
        await _switch(pool, on)
        await _task_with_gate(pool, origin=origin, chat=LARK_USER, kind="user")
        texts, cards = _Poster(), _Poster()
        assert await GateCourier(texts, post_card=cards).run_once() == 1
        assert cards.posts == [] and [type(p["text"]) for p in texts.posts] == [str]
        assert await pool.fetchval("SELECT count(*) FROM card_actions") == 0

    run_db(test, monkeypatch, timeout=30)


def test_a_gate_card_takes_an_option_once_from_the_requester_and_goes_stale_when_the_question_changes(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.gate_courier import GateCourier
    from looper.db import user_gates

    async def test(pool: Any) -> None:
        await _switch(pool)
        await _task_with_gate(pool, origin=BOT, chat=LARK_USER, kind="user")
        texts, cards = _Poster(), _Poster()
        courier = GateCourier(texts, post_card=cards)
        await courier.run_once()
        assert texts.posts == []
        card = cards.posts[0]["text"]
        assert card.text.startswith("⏸ 需要你决定 · settings file\nstorage\n1. Which database?")
        assert [(b.label, b.style) for b in card.buttons] == [
            ("PostgreSQL", "primary"), ("SQLite", "default"), ("按现在的结论结束", "default")]
        bus = _Bus()
        monkeypatch.setitem(card_actions._handlers, "gate",
                            functools.partial(card_actions.press_gate, bus=bus, has_planner=lambda task_id: True))

        # Sent to the user's open_id; the channel notes the message it became and its direct chat.
        await card_actions.bind(card, "om_101", "oc_p2p")  # what the channel notes once sent (``_Poster``'s first id)

        async def press(value: str, presser: str = LARK_USER, message_id: str = "om_101") -> card_actions.Answer:
            return await card_actions.press(card_actions.Press(BOT, presser, value, chat="oc_p2p",
                                                               message_id=message_id))

        assert (await press(card.buttons[0].value, message_id="")).toast == "这张卡片只能在发出它的会话里操作"
        assert (await press(card.buttons[0].value, OTHER)).toast == "只有发起人能回答"
        answer = await press(card.buttons[0].value)
        assert (answer.level, answer.card.text.rsplit("\n\n", 1)[1]) == (
            "success", "✅ 你选了：PostgreSQL。已交给 settings file 的 g1")
        thread = await user_gates.thread("step-g1", pool=pool)
        assert [(m["text"], m["agenda_item"], m["option"], m["via"]) for m in thread if m["role"] == "user"] == [
            ("Which database?：PostgreSQL", "q1", "PostgreSQL", "button:lark_bot")]
        assert [envelope.message_type for envelope in bus.published] == [MessageType.GATE_MESSAGE]
        assert (await press(card.buttons[1].value)).toast == "已处理"

        await _say("task-a", "step-g1", "reminder", "still waiting", state="discussing")
        await courier.run_once()
        reminder = cards.posts[-1]
        assert reminder["reply_to"] == "om_101" and reminder["text"].buttons
        await card_actions.bind(reminder["text"], "om_102", "oc_p2p")
        await _say("task-a", "step-g1", "updated", "only managed ones", state="discussing", agenda=[
            {"id": "q1", "question": "Which database?", "options": ["Aurora", "Cloud SQL"], "status": "open",
             "answer": ""}])
        assert (await press(reminder["text"].buttons[0].value, message_id="om_102")).toast == "已失效，请看最新的消息"
        assert len([m for m in await user_gates.thread("step-g1", pool=pool) if m["role"] == "user"]) == 1

    run_db(test, monkeypatch, timeout=30)


def test_a_database_without_the_table_gets_it_on_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import connection

    async def test(pool: Any) -> None:
        async with dropped_tables(pool, "card_actions"):
            await pool.execute("DELETE FROM schema_migrations WHERE name = 'card_actions'")
            async with pool.acquire() as conn:
                assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert await pool.fetchval("SELECT to_regclass('card_actions') IS NOT NULL")

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("waiting", ["typed", "button"])
def test_a_typed_confirmation_and_a_press_that_race_run_the_call_exactly_once(
        monkeypatch: pytest.MonkeyPatch, waiting: str) -> None:
    """One side waits between finding the confirmation in memory and claiming its card while the other runs: the
    other wins the claim and runs the call, the waiting one loses it, and neither takes the confirmation for
    lapsed (which would run it zero times)."""
    from looper.db import card_actions as db_cards

    async def test(pool: Any) -> None:
        project = await _sample(pool)
        await _switch(pool)
        ops = _ops()
        asked, turn = await _ask(ops)
        following = _turn("om_next")
        assert asked["confirmation_id"] in ops.begin_turn(following)
        paused = asyncio.Event()
        real = db_cards.claim

        async def claim(token: str, *, via: str, **kwargs: Any) -> bool:
            if via == waiting:
                paused.set()
                await asyncio.sleep(0.2)
            return await real(token, via=via, **kwargs)

        monkeypatch.setattr(db_cards, "claim", claim)

        async def press_handler(row: dict[str, Any], event: card_actions.Press, choice: str) -> card_actions.Answer:
            if waiting == "typed":
                await paused.wait()
            return await ops.press(row, event, choice)

        monkeypatch.setitem(card_actions._handlers, "confirmation", press_handler)

        async def typed() -> dict[str, Any]:
            if waiting == "button":
                await paused.wait()
            return await ops.call("looper_confirm", {"confirmation_id": asked["confirmation_id"]})

        async def pressed() -> card_actions.Answer:
            answer = await card_actions.press(card_actions.Press(BOT, ASKER, _value(turn), chat="oc_dm"))
            if answer.later is not None:
                await answer.later
            return answer

        typed_answer, press_answer = await asyncio.gather(typed(), pressed())
        ran = await pool.fetchval("SELECT count(*) FROM manager_actions WHERE tool = 'looper_projects' "
                                  "AND status = 'ok'")
        assert ran == 1 and not await _exists(pool, project)
        winner = "button" if waiting == "typed" else "typed"
        assert typed_answer.get("status") == ("ok" if winner == "typed" else "error")
        assert press_answer.toast == ("已确认，正在执行" if winner == "button" else "已处理")
        assert (await _card_row(pool))[1:] == (winner, "confirm", "ok")

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize(("channel", "channel_id", "chat", "other"), [
    ("lark_bot", BOT, "oc_dm", "oc_team"), ("telegram", "channel-telegram", "4242", "-1004242")])
def test_a_card_acts_only_where_it_was_sent(monkeypatch: pytest.MonkeyPatch, channel: str, channel_id: str,
                                            chat: str, other: str) -> None:
    """The asker's own token pressed from another chat, from a copy of the card's message, or from a message with
    the card's id in another chat claims nothing; from the card's chat and message, it runs."""
    async def test(pool: Any) -> None:
        project = await _sample(pool)
        await _switch(pool)
        ops = _ops()
        monkeypatch.setitem(card_actions._handlers, "confirmation", ops.press)
        turn = Turn(channel=channel, chat=chat, message_id="m-ask", sender=ASKER, channel_id=channel_id, speaker=SpeakerKind.OPERATOR)
        ops.begin_turn(turn)
        await ops.call("looper_projects", DELETE)
        ops.end_turn(turn)

        async def press(where: str, message_id: str = "") -> card_actions.Answer:
            return await card_actions.press(card_actions.Press(channel_id, ASKER, _value(turn), chat=where,
                                                               message_id=message_id))

        elsewhere = await press(other)
        assert (elsewhere.toast, elsewhere.level, elsewhere.later) == ("这张卡片只能在发出它的会话里操作", "error", None)
        assert await pool.fetchval("SELECT handled_at FROM card_actions") is None and await _exists(pool, project)
        await card_actions.bind(card_actions.Card.from_dict(turn.cards[0]), "m-card", chat)
        assert (await press(chat, "m-copy")).toast == "这张卡片只能在发出它的会话里操作"
        # A Telegram message id is unique only within its chat: the same id in another chat is another message.
        assert (await press(other, "m-card")).toast == "这张卡片只能在发出它的会话里操作"
        assert await pool.fetchval("SELECT handled_at FROM card_actions") is None
        answer = await press(chat, "m-card")
        await answer.later
        assert not await _exists(pool, project)

    run_db(test, monkeypatch, timeout=30)


def test_a_claim_that_waited_for_the_rows_lock_past_the_expiry_does_not_handle_it(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    from looper.db import card_actions as db_cards

    async def test(pool: Any) -> None:
        await db_cards.create(token="tok", kind="gate", subject="step", digest="d", payload={}, manager_id=None,
                              channel_id=BOT, chat="oc_dm", allowed=[], ttl=timedelta(milliseconds=600), pool=pool)
        async with pool.acquire() as holder:
            transaction = holder.transaction()
            await transaction.start()
            await holder.execute("SELECT 1 FROM card_actions WHERE token = 'tok' FOR UPDATE")
            claim = asyncio.create_task(db_cards.claim("tok", by=ASKER, via="button", choice="end",
                                                       outcome="answered", pool=pool))
            await asyncio.sleep(1.0)  # the claim waits for the lock while the card expires
            assert not claim.done()
            await transaction.rollback()
            assert await claim is False
        assert await pool.fetchval("SELECT handled_at FROM card_actions WHERE token = 'tok'") is None

    run_db(test, monkeypatch, timeout=30)
