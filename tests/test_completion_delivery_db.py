"""How a completion event's message is delivered (manager completion ledger RFC, C3): its typed delivery state
(``pending`` → ``sending`` → ``sent`` | ``failed`` | ``uncertain``), written ``sending`` before the channel is asked,
through the real Telegram and Lark adapters (Telegram's HTTP API and lark-cli are faked; nothing reaches a chat).

Covered here: a schedule run's message goes to the chat its event recorded, whatever the schedule names later; a
channel's unknown outcome (a part sent, a time-out) is never sent again; an error before any send is retried; the one
claim keeps recovery and a tick from sending a notice twice; a send whose bookkeeping failed is never sent again; the
default off path reads nothing new.

Real PostgreSQL; runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from telegram import Bot
from telegram.error import TimedOut
from telegram.request import BaseRequest

from looper.agents import chat_settings, completion_events
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.channels.base import PostOutcome
from looper.agents.channels.registry import ChannelRegistry
from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.completion_service import CompletionService
from looper.agents.scheduler import Scheduler
from looper.db import manager_events as store, schedules as db_schedules
from tests.db_support import DSN
from tests.schedule_support import TELEGRAM_CHAT, ScheduleEnv, run_scheduled

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

OTHER_CHAT = "1000002"  # a second allowed Telegram chat


@pytest.fixture(autouse=True)
def turns_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.DM_ONLY)
    yield
    completion_events.set_scheduler(None)


class TelegramHTTP(BaseRequest):
    """Telegram's Bot API over HTTP, faked: it answers ``getMe`` and ``sendMessage`` as Telegram does, records each
    message sent, and fails the sends ``fail`` names by their number (``timeout``: the answer is lost after the
    request went out; ``reject``: Telegram's 400). ``gate``: each send waits for it."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[str, str]] = []
        self.fail: dict[int, str] = {}
        self.sends = 0
        self.gate: asyncio.Event | None = None
        self.waiting = asyncio.Event()

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    @property
    def read_timeout(self) -> float:
        return 5.0

    async def do_request(self, url: str, method: str, request_data: Any = None, **kwargs: Any) -> tuple[int, bytes]:
        if url.endswith("/getMe"):
            return 200, json.dumps({"ok": True, "result": {"id": 1, "is_bot": True, "first_name": "Looper",
                                                           "username": "looper_bot"}}).encode()
        params = request_data.parameters
        self.sends += 1
        number = self.sends
        if self.gate is not None:
            self.waiting.set()
            await self.gate.wait()
        if self.fail.get(number) == "timeout":
            raise TimedOut("the answer was lost")
        if self.fail.get(number) == "reject":
            return 400, b'{"ok": false, "error_code": 400, "description": "Bad Request: chat not found"}'
        self.sent.append((str(params["chat_id"]), params["text"]))
        return 200, json.dumps({"ok": True, "result": {
            "message_id": 100 + number, "date": 0, "chat": {"id": int(params["chat_id"]), "type": "private"},
            "text": params["text"]}}).encode()


async def _telegram(env: ScheduleEnv) -> TelegramHTTP:
    """The real Telegram adapter on faked HTTP, as the server's channel registry and the scheduler's poster (and its
    sender, the path the off switch keeps); both chats allowed."""
    from looper.agents.serve import seed_channel_configs

    http = TelegramHTTP()
    bot = Bot("123:SYNTHETIC", request=http, get_updates_request=http)
    await bot.initialize()
    adapter = TelegramChannelAdapter(bot_token="123:SYNTHETIC", channel_id="channel-telegram",
                                     allowlist=SenderAllowlist("channel-telegram",
                                                               frozenset({TELEGRAM_CHAT, OTHER_CHAT})))
    adapter._app = SimpleNamespace(bot=bot)  # what start() sets, without polling Telegram
    registry = ChannelRegistry(env.bus)
    registry._channels["channel-telegram"] = adapter
    env.scheduler = Scheduler(bus=env.bus, clock=env.clock, sender=registry.notify, poster=registry.post,
                              day_zone="America/Los_Angeles", runtime_root=env.runtime_root)
    env.scheduler._commands.prepare()
    completion_events.set_scheduler(env.scheduler)
    await seed_channel_configs(env.pool)
    await env.pool.execute(
        "UPDATE channel_configs SET config = config || $1::jsonb WHERE channel_id = 'channel-telegram'",
        json.dumps({"allowed_user_ids": [TELEGRAM_CHAT, OTHER_CHAT], "operator_sender_id": TELEGRAM_CHAT}))
    return http


async def _failed_run(env: ScheduleEnv, *, notify: str = "failures", status: str = "failed",
                      chat: str = TELEGRAM_CHAT) -> tuple[dict[str, Any], dict[str, Any]]:
    schedule = await env.schedule(notify=notify, notify_target={"channel": "telegram", "chat": chat})
    run = await db_schedules.get_run(await env.scheduler.run_now(schedule["schedule_id"]))
    await env.end_task(run["task_id"], status, summary="the report could not be written")
    await env.scheduler.idle()
    return schedule, run


async def _events(env: ScheduleEnv) -> list[dict[str, Any]]:
    return [dict(row) for row in await env.pool.fetch("SELECT * FROM manager_events ORDER BY id")]


async def _outbox(env: ScheduleEnv) -> list[dict[str, Any]]:
    return [dict(row) for row in await env.pool.fetch(
        "SELECT chat, text, status, manager_event_id FROM schedule_notifications WHERE kind = 'run' "
        "ORDER BY created_at")]


def _service(env: ScheduleEnv) -> CompletionService:
    return CompletionService(bus=env.bus, manager_running=lambda manager_id: True,
                             running_managers=lambda: ["manager"])


def _later(seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


async def _expire(env: ScheduleEnv) -> None:
    """The service's pass once the events waited 15 minutes: they fall back and their notices are delivered."""
    await _service(env).run_once(now=_later(store.QUEUE_SECONDS + 1))


# --- A run's message, through the real Telegram adapter ---


def test_a_failed_runs_message_waits_for_the_turn_and_its_reply_is_sent_and_recorded(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        http = await _telegram(env)
        schedule, run = await _failed_run(env)
        assert http.sent == [] and await _outbox(env) == []  # no code notice: the completion turn tells it
        (row,) = await _events(env)
        assert (row["origin"], row["target_kind"], row["state"]) == ("schedule", "dm", "pending")
        assert json.loads(row["payload"])["result"]["lead"] == "the report could not be written"
        rows = await store.claim("manager", "channel-telegram", TELEGRAM_CHAT, uuid.uuid4())
        message = completion_events.completion_message(rows, channel_line="Channel: a Telegram direct message",
                                                       more={})
        assert f'1. {run["task_id"]}: failed; a run of the schedule "{schedule["name"]}", whose messages come to ' \
               "this chat." in message
        outcome = await completion_events.send_reply(None, None, rows, "日报没写出来。要我再跑一次吗？")
        assert outcome == (PostOutcome.SENT, None)
        assert http.sent == [(TELEGRAM_CHAT, "日报没写出来。要我再跑一次吗？")]
        assert await _outbox(env) == [{"chat": TELEGRAM_CHAT, "text": "日报没写出来。要我再跑一次吗？",
                                       "status": "sent", "manager_event_id": row["id"]}]

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_successful_run_under_notify_failures_records_no_event(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        http = await _telegram(env)
        await _failed_run(env, status="completed")
        assert http.sent == [] and await _events(env) == []

    run_scheduled(test, monkeypatch, tmp_path)


def test_with_the_switch_off_a_runs_message_goes_as_before_with_no_event_in_the_outbox(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns.OFF)

    async def test(env: ScheduleEnv):
        http = await _telegram(env)
        await _failed_run(env)
        assert len(http.sent) == 1
        (note,) = await _outbox(env)
        assert (note["manager_event_id"], note["status"]) == (None, "sent")
        assert [(r["state"], r["reason"], r["delivery"]) for r in await _events(env)] == [("skipped", "off", None)]

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("turns", ["off", "dm_only"])
def test_a_tasks_result_that_cannot_be_read_never_costs_the_runs_message(monkeypatch, tmp_path, turns):
    from types import SimpleNamespace as Namespace

    from looper.agents import schedule_notify
    from looper.db import tasks as db_tasks

    async def get_task(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("the database went away")

    def failing() -> Any:
        return Namespace(**{name: getattr(db_tasks, name) for name in dir(db_tasks)
                            if not name.startswith("__") and name != "get_task"}, get_task=get_task)

    monkeypatch.setattr(chat_settings, "_completion_turns", chat_settings.CompletionTurns(turns))
    monkeypatch.setattr(schedule_notify, "db_tasks", failing())
    monkeypatch.setattr(completion_events, "db_tasks", failing())

    async def test(env: ScheduleEnv):
        http = await _telegram(env)
        await _failed_run(env)
        assert len(http.sent) == 1  # the run's message, as before
        assert [(r["state"], r["reason"]) for r in await _events(env)] == (
            [("skipped", "off")] if turns == "off" else [])  # on: no event could be written; the message went

    run_scheduled(test, monkeypatch, tmp_path)


# --- Review findings, one each, through the real adapters ---


def test_a_batch_goes_to_the_chat_its_events_recorded_even_when_the_schedule_moves_meanwhile(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        http = await _telegram(env)
        schedule, _ = await _failed_run(env)
        check = env.scheduler.run_target_ok

        async def moved_after_the_check(*args: Any) -> bool:
            ok = await check(*args)  # the target is still the recorded chat here; then it moves
            await env.pool.execute("UPDATE schedules SET notify_chat = $1 WHERE id = $2", OTHER_CHAT,
                                   schedule["schedule_id"])
            return ok

        monkeypatch.setattr(env.scheduler, "run_target_ok", moved_after_the_check)
        rows = await store.claim("manager", "channel-telegram", TELEGRAM_CHAT, uuid.uuid4())
        assert (await completion_events.send_reply(None, None, rows, "日报没写出来。"))[0] is PostOutcome.SENT
        assert http.sent == [(TELEGRAM_CHAT, "日报没写出来。")]  # never the chat the schedule names later
        assert [note["chat"] for note in await _outbox(env)] == [TELEGRAM_CHAT]

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_batch_whose_schedule_moved_before_the_check_is_not_sent_and_each_notice_goes_to_its_recorded_chat(
        monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        http = await _telegram(env)
        first, _ = await _failed_run(env)
        await _failed_run(env)
        await env.pool.execute("UPDATE schedules SET notify_chat = $1 WHERE id = $2", OTHER_CHAT,
                               first["schedule_id"])
        batch = uuid.uuid4()
        rows = await store.claim("manager", "channel-telegram", TELEGRAM_CHAT, batch)
        with pytest.raises(completion_events.TargetChanged):
            await completion_events.send_reply(None, None, rows, "两份日报都没写出来。")
        assert http.sent == []
        await completion_events.fall_back(None, None, reason="target_changed", batch=batch)
        assert sorted(chat for chat, _ in http.sent) == [TELEGRAM_CHAT, TELEGRAM_CHAT]  # each to its recorded chat
        assert {(r["state"], r["reason"], r["delivery"]) for r in await _events(env)} == {
            ("fallback", "target_changed", "sent")}

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("failure", ["timeout", "second_part"])
def test_a_telegram_send_that_may_have_gone_is_uncertain_and_never_sent_again(monkeypatch, tmp_path, failure):
    async def test(env: ScheduleEnv):
        http = await _telegram(env)
        await _failed_run(env)
        long_text = "x" * 3990 + "\n" + "y" * 100  # two Telegram messages
        await env.pool.execute("UPDATE manager_events SET fallback = jsonb_set(fallback, '{0,text}', $1::jsonb)",
                               json.dumps(long_text))
        http.fail = {1: "timeout"} if failure == "timeout" else {2: "timeout"}
        await _expire(env)
        (row,) = await _events(env)
        assert (row["state"], row["delivery"], row["sent_at"]) == ("fallback", "uncertain", None)
        sends = http.sends
        http.fail = {}
        for later in (store.RETRY_MAX_SECONDS, 24 * 3600):  # later passes, a restart: never again
            await _service(env).run_once(now=_later(later))
        await _service(env).start()
        assert http.sends == sends and (await _events(env))[0]["delivery"] == "uncertain"

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_lark_send_with_no_answer_is_uncertain_and_never_sent_again(monkeypatch, tmp_path, lark_cli_stand_in):
    from lark_cli_stand_in import OPEN_ID

    from looper.agents.channels.lark import LarkChannelAdapter
    from looper.agents.serve import seed_channel_configs

    person = "ou_lostservertimeout"  # the stand-in's send to this person gets the platform's time-out

    async def test(env: ScheduleEnv):
        adapter = LarkChannelAdapter(profile="other", runtime_root=tmp_path,
                                     allowlist=SenderAllowlist("channel-lark", frozenset({OPEN_ID, person})))
        registry = ChannelRegistry(env.bus)
        registry._channels["channel-lark"] = adapter
        env.scheduler = Scheduler(bus=env.bus, clock=env.clock, sender=registry.notify, poster=registry.post,
                                  day_zone="America/Los_Angeles", runtime_root=env.runtime_root)
        env.scheduler._commands.prepare()
        completion_events.set_scheduler(env.scheduler)
        await seed_channel_configs(env.pool)
        await env.pool.execute("UPDATE channel_configs SET enabled = TRUE, config = config || $1::jsonb "
                               "WHERE channel_id = 'channel-lark'",
                               json.dumps({"profile": "other", "allowed_open_ids": [OPEN_ID, person],
                                           "operator_sender_id": person}))
        schedule = await env.schedule(notify="failures", notify_target={"channel": "lark", "chat": person})
        run = await db_schedules.get_run(await env.scheduler.run_now(schedule["schedule_id"]))
        await env.end_task(run["task_id"], "failed", summary="no data")
        await env.scheduler.idle()
        await _expire(env)
        (row,) = await _events(env)
        assert (row["delivery"], row["sent_at"]) == ("uncertain", None)
        sent = [line for line in lark_cli_stand_in.read_text().splitlines() if "+messages-send" in line]
        assert len(sent) == 1
        await _service(env).run_once(now=_later(24 * 3600))
        assert [line for line in lark_cli_stand_in.read_text().splitlines() if "+messages-send" in line] == sent

    run_scheduled(test, monkeypatch, tmp_path)


def test_an_error_before_any_send_is_retried_and_one_the_channel_refused_too(monkeypatch, tmp_path):
    from looper.agents import schedules

    real, failures = schedules.notify_targets, []

    async def notify_targets(*args: Any, **kwargs: Any) -> Any:
        if not failures:
            failures.append(1)
            raise ConnectionError("the database went away")
        return await real(*args, **kwargs)

    async def test(env: ScheduleEnv):
        http = await _telegram(env)
        await _failed_run(env)
        monkeypatch.setattr(schedules, "notify_targets", notify_targets)
        await _expire(env)
        (row,) = await _events(env)
        assert (row["delivery"], row["delivery_attempts"], http.sends) == ("failed", 1, 0)  # never asked the channel
        http.fail = {1: "reject"}  # then Telegram refuses it once: nothing was sent
        await _service(env).run_once(now=row["delivery_at"] + timedelta(seconds=1))
        (row,) = await _events(env)
        assert (row["delivery"], row["delivery_attempts"], http.sent) == ("failed", 2, [])
        await _service(env).run_once(now=row["delivery_at"] + timedelta(seconds=1))
        (row,) = await _events(env)
        assert (row["delivery"], len(http.sent)) == ("sent", 1) and row["sent_at"] is not None

    run_scheduled(test, monkeypatch, tmp_path)


def test_recovery_and_a_tick_never_send_a_notice_another_pass_is_sending(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        http = await _telegram(env)
        await _failed_run(env)
        restarted = _service(env)  # built when the server started, before this send began
        http.gate = asyncio.Event()
        sending = asyncio.create_task(_expire(env))
        await asyncio.wait_for(http.waiting.wait(), 10)
        assert (await _events(env))[0]["delivery"] == "sending"
        # Meanwhile: a Manager's recovery, the service's start and another tick.
        await completion_events.recover(env.bus, completion_events.manager_address("manager"))
        await restarted.start()
        await restarted.stop()
        await _service(env).run_once(now=_later(24 * 3600))
        http.gate.set()
        await asyncio.wait_for(sending, 10)
        assert http.sends == 1 and len(http.sent) == 1
        assert (await _events(env))[0]["delivery"] == "sent"

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_notice_that_went_but_whose_settlement_failed_is_never_sent_again(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        http = await _telegram(env)
        await _failed_run(env)

        async def unwritable(*args: Any, **kwargs: Any) -> bool:
            raise ConnectionError("the database went away after the send")

        real = store.settle_delivery
        monkeypatch.setattr(store, "settle_delivery", unwritable)
        await _expire(env)
        assert len(http.sent) == 1 and (await _events(env))[0]["delivery"] == "sending"
        monkeypatch.setattr(store, "settle_delivery", real)
        await _service(env).run_once(now=_later(store.RETRY_MAX_SECONDS))  # any lease or backoff long past
        assert len(http.sent) == 1
        monkeypatch.setattr(store, "SENDING_STALE_SECONDS", 0)
        await _service(env).run_once()
        assert len(http.sent) == 1 and (await _events(env))[0]["delivery"] == "uncertain"

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_sent_reply_whose_outbox_row_failed_is_recorded_once_and_not_sent_again(monkeypatch, tmp_path):
    real, failed = db_schedules.insert_notification, []

    async def insert_notification(*args: Any, **kwargs: Any) -> str:
        if kwargs.get("manager_event_id") is not None and not failed:
            failed.append(1)
            raise ConnectionError("the database went away after the send")
        return await real(*args, **kwargs)

    monkeypatch.setattr(db_schedules, "insert_notification", insert_notification)

    async def test(env: ScheduleEnv):
        http = await _telegram(env)
        await _failed_run(env)
        (row,) = await _events(env)
        rows = await store.claim("manager", "channel-telegram", TELEGRAM_CHAT, uuid.uuid4())
        assert (await completion_events.send_reply(None, None, rows, "日报没写出来。"))[0] is PostOutcome.SENT
        assert http.sent == [(TELEGRAM_CHAT, "日报没写出来。")] and failed == [1]
        assert await _outbox(env) == [{"chat": TELEGRAM_CHAT, "text": "日报没写出来。", "status": "sent",
                                       "manager_event_id": row["id"]}]

    run_scheduled(test, monkeypatch, tmp_path)



# --- A send whose record failed, or that answered late (review round 3) ---


@pytest.mark.parametrize("failures", [1, 3])
def test_a_managers_reply_that_went_but_whose_record_failed_is_recorded_again_and_never_sent_again(
        monkeypatch, tmp_path, failures):
    """The Manager's completion turn, on a loopback model API, replies through the real Telegram adapter; writing
    its delivery then fails ``failures`` times. The record (never the send) is tried again; one never written does
    not hold the chat: the service settles it ``uncertain`` and the chat's next end gets its turn."""
    from looper.agents.message import MessageType
    from tests.api_manager_support import USAGE, _manager
    from tests.fake_model_api import ResponseScript, text_events

    real, calls = store.deliver, []

    async def deliver(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) <= failures:
            raise ConnectionError("the database went away after the send")
        return await real(*args, **kwargs)

    async def tell(chat: Any) -> None:
        await CompletionService(bus=chat.bus, manager_running=lambda manager_id: True,
                                running_managers=lambda: ["manager"]).run_once(now=_later(store.WINDOW_SECONDS + 1))
        queue = chat.bus.get_queue("manager")
        (due,) = [e for e in (queue.get_nowait() for _ in range(queue.qsize()))
                  if e.message_type is MessageType.COMPLETION_DUE]
        await chat.manager._handle(due)

    async def test(env: ScheduleEnv):
        http = await _telegram(env)
        await _failed_run(env)
        root = tmp_path / "manager"
        root.mkdir()
        scripts = [ResponseScript(events=text_events(answer, usage=USAGE)) for answer in ("日报没写出来。", "又没写出来。")]
        async with _manager(env.pool, root, monkeypatch, scripts) as chat:
            monkeypatch.setattr(store, "deliver", deliver)
            await tell(chat)
            assert [text.split("\n")[0] for _, text in http.sent] == ["日报没写出来。"]
            (row,) = await _events(env)
            if failures < 3:  # the second try wrote it
                assert (row["state"], row["delivery"], len(calls)) == ("delivered", "sent", failures + 1)
                return
            assert (row["state"], row["delivery"], len(calls)) == ("running", "sending", 3)
            monkeypatch.setattr(store, "SENDING_STALE_SECONDS", 0)
            await _service(env).run_once()
            (row,) = await _events(env)
            assert (row["state"], row["delivery"], row["sent_at"]) == ("delivered", "uncertain", None)
            assert len(http.sent) == 1  # never sent again, nor its notice
            await _failed_run(env)  # the chat's next end: its turn is not held by the first
            await tell(chat)
            assert [text.split("\n")[0] for _, text in http.sent] == ["日报没写出来。", "又没写出来。"]
            assert [(r["state"], r["delivery"]) for r in await _events(env)] == [
                ("delivered", "uncertain"), ("delivered", "sent")]

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("message", ["notice", "reply"])
def test_a_send_that_answers_sent_after_it_went_stale_is_recorded_sent_and_never_sent_again(
        monkeypatch, tmp_path, message):
    """A live send that takes longer than ``SENDING_STALE_SECONDS`` is settled ``uncertain`` meanwhile; when Telegram
    then answers that it was sent, the same send (its claim) records ``sent``. Another claim's answer changes
    nothing, and nothing is sent again."""
    async def test(env: ScheduleEnv):
        http = await _telegram(env)
        await _failed_run(env)
        http.gate = asyncio.Event()
        if message == "notice":
            sending = asyncio.create_task(_expire(env))
        else:
            rows = await store.claim("manager", "channel-telegram", TELEGRAM_CHAT, uuid.uuid4())

            async def reply() -> None:
                outcome, error = await completion_events.send_reply(None, None, rows, "日报没写出来。")
                assert outcome is PostOutcome.SENT
                assert await completion_events.record_reply(rows, outcome, error, record_id=None)

            sending = asyncio.create_task(reply())
        await asyncio.wait_for(http.waiting.wait(), 10)
        monkeypatch.setattr(store, "SENDING_STALE_SECONDS", 0)
        await _service(env).run_once()  # the send under way is past the stale age
        (row,) = await _events(env)
        assert (row["state"], row["delivery"]) == ("fallback" if message == "notice" else "delivered", "uncertain")
        http.gate.set()
        await asyncio.wait_for(sending, 10)
        (row,) = await _events(env)
        assert (row["delivery"], row["sent_at"] is not None, row["delivery_error"]) == ("sent", True, None)
        assert not await store.settle_delivery(row["id"], "failed", claim=uuid.uuid4())
        await _service(env).run_once(now=_later(24 * 3600))
        assert http.sends == 1 and (await _events(env))[0]["delivery"] == "sent"

    run_scheduled(test, monkeypatch, tmp_path)
