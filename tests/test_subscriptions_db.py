"""Subscriptions on a real PostgreSQL: what a run reads, what it pushes and when, what moves its cursor, what the
operator has to confirm, and what a restart does to a run in flight.

The processor is scripted (``processor_says``): its own launch is covered by ``test_processor_db.py``. Nothing is sent
anywhere: the channels' ``post`` is ``Poster``. Runs only with a disposable LOOPER_TEST_DATABASE_URL.
"""

from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from looper.agents import processor, subscription_runs
from looper.agents.channels.base import PostOutcome, PostReceipt
from looper.agents.task_notice import TargetKind
from looper.db import schedules as db_schedules, subscriptions as db_subs
from tests.db_support import DSN, run_db, v1_api
from tests.lark_cli_stand_in import lark_cli_stand_in  # noqa: F401  (a fixture)
from tests.schedule_support import ScheduleEnv, run_scheduled, with_sender
from tests.subscription_support import (DM, GROUP, SOURCE, INSTRUCTIONS, SUBSCRIPTION, UNBOUND_GROUP, Poster, add, lark_targets, minutes_ago, person, processor_says, scheduler_with, source)

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
UTC = timezone.utc
CREATE = "/api/v1/schedule_admin/create"
UPDATE = "/api/v1/schedule_admin/update"


async def started(env: ScheduleEnv, *, target: Any = {"channel": "lark", "chat": DM}, subscription: dict | None = None,
                  enable: bool = True, **fields: Any) -> str:
    """A subscription on the stream (its cursor at the source's end), enabled; its schedule id."""
    await source(env.pool)
    body = {"kind": "subscription", "requirement": INSTRUCTIONS, "run_budget_usd": 0.5, "notify": "failures",
            "subscription": {**SUBSCRIPTION, **(subscription or {})}, **({"notify_target": target} if target else {}),
            **fields}
    sid = (await env.schedule(**body))["schedule_id"]
    if enable:
        await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {"enabled": True}})
    return sid


async def version_of(env: ScheduleEnv, sid: str) -> str:
    view = (await env.ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))["schedule"]
    return view["subscription"]["config_version"]


async def confirm(env: ScheduleEnv, sid: str) -> None:
    """The operator confirms what the schedule's view shows."""
    await env.ok("POST", "/api/v1/schedule_admin/confirm_target",
                 {"schedule_id": sid, "config_version": await version_of(env, sid)})


async def fire(env: ScheduleEnv, sid: str) -> dict[str, Any]:
    """Make the schedule due, run one scheduler pass and the run it started; the run's row and its subscription's."""
    await env.due(sid)
    await env.scheduler.tick()
    await env.scheduler.idle()
    return await latest(env, sid)


async def latest(env: ScheduleEnv, sid: str) -> dict[str, Any]:
    row = dict(await env.pool.fetchrow("SELECT * FROM schedule_runs WHERE schedule_id = $1 "
                                       "ORDER BY created_at DESC, id DESC LIMIT 1", sid))
    stream = await env.pool.fetchrow("SELECT * FROM subscription_runs WHERE run_id = $1", row["id"])
    return {**row, "stream": dict(stream) if stream else None}


async def cursor(env: ScheduleEnv, sid: str) -> int:
    return await env.pool.fetchval("SELECT cursor_seq FROM subscriptions WHERE schedule_id = $1", sid)


async def confirmed_at(env: ScheduleEnv, sid: str) -> Any:
    return await env.pool.fetchval("SELECT target_confirmed_at FROM subscriptions WHERE schedule_id = $1", sid)


def scenario(test, monkeypatch, tmp_path, *, reply: str = "- 《Ops room》: someone spilled coffee"):
    """Run ``test(env, poster, fake)`` with the Lark targets, a scheduler that pushes through ``poster`` and a
    processor that answers ``reply``."""
    fake = processor_says(monkeypatch, reply)
    monkeypatch.setattr(subscription_runs, "RETRY_DELAY_SECONDS", 0)

    async def body(env: ScheduleEnv) -> None:
        await lark_targets(env.pool)
        poster = Poster()
        scheduler_with(env, poster=poster)
        await test(env, poster, fake)

    run_scheduled(body, monkeypatch, tmp_path)


# --- Reading the stream ---


def test_a_window_with_nothing_selected_starts_no_agent_and_the_firings_are_counted_together(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        # Only what the filter drops arrives: a direct chat, a message that @-mentions the operator.
        await add(env.pool, "private", chat_id="oc_dm", kind="p2p", sent_at=minutes_ago(env, 3))
        await add(env.pool, "for you", mentions_me=True, sent_at=minutes_ago(env, 3))
        first = await fire(env, sid)
        assert first["status"] == "skipped_gate" and fake.calls == [] and poster.sent == []
        assert await cursor(env, sid) == await env.pool.fetchval("SELECT committed_seq FROM event_sources")
        assert first["stream"] is None  # no window was run: nothing was started
        second = await fire(env, sid)
        assert second["id"] == first["id"] and second["firings"] == 2  # counted on the day's latest skip
        assert await env.pool.fetchval("SELECT count(*) FROM schedule_runs WHERE schedule_id = $1", sid) == 1
        # A skip that found nothing to do is not a failure.
        assert (await env.pool.fetchval("SELECT consecutive_failures FROM schedules WHERE id = $1", sid)) == 0

    scenario(check, monkeypatch, tmp_path)


def test_the_window_is_the_filtered_events_newest_first_under_the_collectors_lines(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        await source(env.pool)
        await person(env.pool, "ou_alice", "Alice")
        sid = await started(env)
        for text, chat_id, kind, extra in [
                ("old group", "oc_ops", "group", {"sent_at": minutes_ago(env, 30)}),
                ("a topic", "oc_topic", "topic", {"sent_at": minutes_ago(env, 20), "chat_name": "Topic room"}),
                ("a bot says", "oc_ops", "group", {"sent_at": minutes_ago(env, 10), "sender": "cli_bot",
                                                   "sender_kind": "app"}),
                # What the filter leaves out:
                ("direct", "oc_dm", "p2p", {"sent_at": minutes_ago(env, 9)}),
                ("for me", "oc_ops", "group", {"sent_at": minutes_ago(env, 8), "mentions_me": True}),
                ("history", "oc_ops", "group", {"sent_at": minutes_ago(env, 7), "origin": "history"}),
                ("import", "oc_ops", "group", {"sent_at": minutes_ago(env, 6), "origin": "import"}),
                ("too old", "oc_ops", "group", {"sent_at": minutes_ago(env, 181)})]:
            await add(env.pool, text, chat_id=chat_id, kind=kind, **extra)
        await confirm(env, sid)
        run = await fire(env, sid)
        assert run["status"] == "completed"
        [call] = fake.calls
        lines = call["input_text"].splitlines()
        assert lines[0] == "消息(共 3 条):"
        assert [line.split(": ", 1)[1] for line in lines[1:]] == ["a bot says", "a topic", "old group"]  # newest first
        assert lines[1].startswith("[") and "《Ops room》 cli_bot: " in lines[1]  # no name known: ten characters of the id
        assert "《Topic room》 ou_alice" not in lines[2] and "《Topic room》 Alice: a topic" in lines[2]
        assert (call["instructions"], call["quiet_word"], call["schedule_id"]) == (INSTRUCTIONS, "SKIP", sid)
        stream = run["stream"]
        assert (stream["events_in_window"], stream["events_selected"], stream["events_too_old"]) == (8, 3, 1)

    scenario(check, monkeypatch, tmp_path)


def test_more_than_the_cap_keeps_the_newest_and_counts_the_rest(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env, subscription={"max_events": 500})
        for number in range(503):
            await add(env.pool, f"message {number}", sent_at=minutes_ago(env, 100) + timedelta(seconds=number))
        await confirm(env, sid)
        run = await fire(env, sid)
        [call] = fake.calls
        lines = call["input_text"].splitlines()
        assert lines[0] == "消息(共 500 条):" and lines[1].endswith("message 502") and lines[-1].endswith("message 3")
        assert run["stream"]["events_over_cap"] == 3 and run["stream"]["events_selected"] == 500

    scenario(check, monkeypatch, tmp_path)


def test_a_source_of_the_looper_bot_never_comes_back_whatever_the_filter_says(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        async def own() -> set[str]:
            return {"cli_ours"}
        scheduler_with(env, poster=poster, own_apps=own)
        sid = await started(env)
        await add(env.pool, "our own push", sender="cli_ours", sender_kind="app", sent_at=minutes_ago(env, 5))
        await add(env.pool, "another bot", sender="cli_other", sender_kind="app", sent_at=minutes_ago(env, 4))
        await confirm(env, sid)
        await fire(env, sid)
        assert [line.split(": ", 1)[1] for line in fake.calls[0]["input_text"].splitlines()[1:]] == ["another bot"]

    scenario(check, monkeypatch, tmp_path)


# --- The reply ---


def test_the_quiet_word_pushes_nothing_and_moves_the_cursor_on(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        top = await add(env.pool, "dull", sent_at=minutes_ago(env, 5))
        fake.reply = " **skip** \n"
        run = await fire(env, sid)
        assert run["status"] == "completed" and run["stream"]["outcome"] == "quiet"
        assert poster.sent == [] and await cursor(env, sid) == top
        assert run["cost_usd"] == pytest.approx(0.01)
        # The next window starts after it: nothing new, no agent.
        calls = len(fake.calls)
        assert (await fire(env, sid))["status"] == "skipped_gate" and len(fake.calls) == calls

    scenario(check, monkeypatch, tmp_path)


def test_a_reply_is_pushed_to_the_confirmed_target_under_its_header_without_mentions(monkeypatch, tmp_path):
    reply = '<at user_id="ou_x">小明</at> 和 @_user_1 吵架了，@所有人 快看 <at id="all"></at>'

    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        top = await add(env.pool, "gossip", sent_at=minutes_ago(env, 5))
        run = await fire(env, sid)
        assert run["status"] == "completed" and run["stream"]["outcome"] == "delivered"
        [sent] = poster.sent
        assert sent["channel_id"] == "channel-lark" and sent["key"] == run["id"]
        assert sent["target"].kind is TargetKind.USER and sent["target"].id == DM and not sent["target"].group
        assert sent["text"] == "🍉 吃瓜速报 · 过去1小时\n\n小明 和  吵架了，所有人 快看"
        assert "<at" not in sent["text"] and "@" not in sent["text"]
        assert await cursor(env, sid) == top
        notification = await env.pool.fetchrow("SELECT * FROM schedule_notifications WHERE run_id = $1", run["id"])
        assert (notification["status"], notification["kind"], notification["chat"]) == ("sent", "run", DM)
        assert run["stream"]["notification_id"] == notification["id"]
        assert run["stream"]["output_text"] == sent["text"] and run["summary"] == sent["text"]
        # No second message about the same run: the run's message is its output.
        assert await env.pool.fetchval("SELECT count(*) FROM schedule_notifications WHERE schedule_id = $1", sid) == 1

    scenario(check, monkeypatch, tmp_path, reply=reply)


def test_a_long_reply_is_pushed_in_several_messages_with_their_own_keys(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        run = await fire(env, sid)
        assert [m["key"] for m in poster.sent] == [run["id"], f"{run['id']}-2"]
        assert all(len(m["text"]) <= 4000 for m in poster.sent)

    scenario(check, monkeypatch, tmp_path, reply=("y" * 99 + "\n") * 60)


def test_a_reply_that_is_only_mentions_is_a_failure_and_leaves_the_cursor(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        before = await cursor(env, sid)
        run = await fire(env, sid)
        assert run["status"] == "failed" and run["stream"]["outcome"] == "output_invalid"
        assert poster.sent == [] and await cursor(env, sid) == before

    scenario(check, monkeypatch, tmp_path, reply='<at id="all"></at>')


# --- Failing ---


@pytest.mark.parametrize("failure, status", [(processor.ProcessorFailure.FAILED, "failed"),
                                             (processor.ProcessorFailure.TIMEOUT, "timed_out"),
                                             (processor.ProcessorFailure.BUDGET, "failed"),
                                             (processor.ProcessorFailure.REFUSED, "failed")])
def test_a_failed_run_leaves_the_cursor_and_the_next_run_takes_its_window_too(monkeypatch, tmp_path, failure, status):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        before = await cursor(env, sid)
        first = await add(env.pool, "first", sent_at=minutes_ago(env, 100))
        fake.failure = failure
        run = await fire(env, sid)
        assert run["status"] == status and run["stream"]["outcome"] == "failed"
        assert failure.value in run["error"] and run["consecutive_failures"] if "consecutive_failures" in run else True
        assert await cursor(env, sid) == before and poster.sent == []
        fake.failure = None
        second = await add(env.pool, "second", sent_at=minutes_ago(env, 5) + timedelta(minutes=10))
        run = await fire(env, sid)
        assert run["status"] == "completed" and (run["stream"]["from_seq"], run["stream"]["to_seq"]) == (before, second)
        assert fake.calls[-1]["input_text"].splitlines()[0] == "消息(共 2 条):"
        assert await cursor(env, sid) == second and first < second

    scenario(check, monkeypatch, tmp_path)


def test_what_is_too_old_by_the_next_run_is_dropped_and_counted(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env, subscription={"max_age_minutes": 60})
        await confirm(env, sid)
        await add(env.pool, "stale", sent_at=minutes_ago(env, 30))
        fake.failure = processor.ProcessorFailure.FAILED
        await fire(env, sid)  # fails; the message is still inside the window of the next run only if it is young enough
        fake.failure = None
        env.clock.now += timedelta(hours=2)
        await add(env.pool, "fresh", sent_at=env.clock.now - timedelta(minutes=2))
        run = await fire(env, sid)
        assert run["status"] == "completed", run["error"]
        assert [line.split(": ", 1)[1] for line in fake.calls[-1]["input_text"].splitlines()[1:]] == ["fresh"]
        assert (run["stream"]["events_too_old"], run["stream"]["events_selected"]) == (1, 1)

    scenario(check, monkeypatch, tmp_path)


def test_a_failed_push_fails_the_run_and_leaves_the_cursor(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        before = await cursor(env, sid)
        poster.fail_with = "chat not found"
        run = await fire(env, sid)
        assert run["status"] == "failed" and "chat not found" in run["error"] and run["stream"]["outcome"] == "failed"
        assert await cursor(env, sid) == before
        assert run["stream"]["output_text"]  # what the processor said stays for the operator to read
        assert await env.pool.fetchval("SELECT count(*) FROM schedule_notifications WHERE status = 'sent'") == 0

    scenario(check, monkeypatch, tmp_path)


# --- A crash ---


async def left_by_a_dead_server(env: ScheduleEnv, run: dict[str, Any], *, sending: int | None = None) -> None:
    """The run as a server that died in its push leaves it: running, its output saved, message ``sending`` as it was."""
    await env.pool.execute("UPDATE schedule_runs SET status = 'running', finished_at = NULL, error = NULL, "
                           "runner_pid = 999999, runner_started = 'a dead server' WHERE id = $1", run["id"])
    await env.pool.execute("UPDATE subscription_runs SET outcome = 'delivering', notification_id = NULL WHERE run_id = $1",
                           run["id"])
    await env.pool.execute("DELETE FROM schedule_notifications WHERE run_id = $1", run["id"])
    await env.pool.execute("UPDATE subscriptions SET cursor_seq = $2 WHERE schedule_id = $1",
                           run["schedule_id"], run["stream"]["from_seq"])
    if sending is not None:
        await env.pool.execute("UPDATE subscription_deliveries SET state = 'sending' WHERE run_id = $1 AND segment = $2",
                               run["id"], sending)


@pytest.mark.parametrize("dedupe", [True, False], ids=["key-remembered", "key-expired-or-ignored"])
def test_a_message_that_was_being_sent_when_the_server_died_is_uncertain_and_never_sent_again(monkeypatch, tmp_path, dedupe):
    async def check(env, poster, fake):
        poster.dedupe = dedupe  # Lark forgets a key after an hour; Telegram has none: the channel cannot be relied on
        sid = await started(env)
        await confirm(env, sid)
        top = await add(env.pool, "gossip", sent_at=minutes_ago(env, 5))
        run = await fire(env, sid)
        assert len(poster.sent) == 1
        await left_by_a_dead_server(env, run, sending=1)
        scheduler_with(env, poster=poster)  # the restarted server, however long after
        await env.scheduler.tick()
        await env.scheduler.idle()
        again = await latest(env, sid)
        assert again["id"] == run["id"] and again["status"] == "completed" and again["stream"]["outcome"] == "uncertain"
        assert poster.calls == 1 and len(poster.sent) == 1  # the channel was not asked again
        [segment] = await db_subs.list_deliveries(run["id"])
        assert segment["state"] == "uncertain" and "stopped" in segment["error"]
        assert await cursor(env, sid) == top  # the window is not read twice
        view = (await env.ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))["schedule"]
        assert view["subscription"]["uncertain_deliveries"] == 1
        runs = (await env.ok("POST", "/api/v1/schedule_admin/runs", {"schedule_id": sid}))["runs"]
        assert runs[0]["subscription"]["deliveries"][0]["state"] == "uncertain"

    scenario(check, monkeypatch, tmp_path)


def test_a_channel_that_raises_leaves_the_message_uncertain(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        top = await add(env.pool, "gossip", sent_at=minutes_ago(env, 5))
        poster.break_after = 1
        run = await fire(env, sid)
        assert run["status"] == "completed" and run["stream"]["outcome"] == "uncertain" and poster.calls == 1
        assert await cursor(env, sid) == top

    scenario(check, monkeypatch, tmp_path)


def test_a_message_the_channel_refused_is_sent_again_under_the_same_run_up_to_its_attempts(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        poster.fail_segments = {1: 1, 2: 1}  # the first two asks are refused
        run = await fire(env, sid)
        assert run["status"] == "completed" and run["stream"]["outcome"] == "delivered"
        [segment] = await db_subs.list_deliveries(run["id"])
        assert (segment["state"], segment["attempts"]) == ("sent", 3) and len(poster.sent) == 1

    scenario(check, monkeypatch, tmp_path)


def test_a_push_cut_short_after_a_message_was_sent_is_not_started_again_by_the_next_run(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        top = await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        poster.fail_segments = {2: 1, 3: 1, 4: 1}  # the second message is refused every time
        run = await fire(env, sid)
        assert run["status"] == "failed" and run["stream"]["outcome"] == "failed" and "message 2 of 2" in run["error"]
        states = [d["state"] for d in await db_subs.list_deliveries(run["id"])]
        assert states == ["sent", "failed"] and len(poster.sent) == 1
        assert await cursor(env, sid) == top  # one message is out: its window is not read again
        again = await fire(env, sid)
        assert again["id"] != run["id"] and again["status"] == "skipped_gate" and len(poster.sent) == 1

    scenario(check, monkeypatch, tmp_path, reply=("y" * 99 + "\n") * 60)


def test_a_push_that_sent_nothing_leaves_its_window_for_the_next_run(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        before = await cursor(env, sid)
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        poster.fail_with = "down"
        run = await fire(env, sid)
        assert run["status"] == "failed" and await cursor(env, sid) == before
        assert poster.calls == db_subs.DELIVERY_ATTEMPTS

    scenario(check, monkeypatch, tmp_path)


@pytest.mark.parametrize("what", ["target", "never", "binding", "cron", "allowlist"])
def test_every_message_is_checked_against_what_the_run_started_with_and_nothing_else_is_pushed(monkeypatch, tmp_path, what):
    async def check(env, poster, fake):
        sid = await started(env, target={"channel": "lark", "chat": GROUP if what == "binding" else DM})
        await confirm(env, sid)
        await add(env.pool, "gossip", sent_at=minutes_ago(env, 5))

        async def change() -> None:
            if what == "target":
                await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {"notify_target": {"channel": "lark", "chat": GROUP}}})
            elif what == "never":
                await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {"notify": "never"}})
            elif what == "cron":
                await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {"cron": "10 * * * *"}})
            elif what == "binding":
                await env.pool.execute("DELETE FROM manager_chat_bindings")
            else:
                await env.pool.execute("UPDATE channel_configs SET config = jsonb_set(config, '{allowed_open_ids}', '[]') "
                                       "WHERE channel_id = 'channel-lark'")
        fake.during = change
        run = await fire(env, sid)
        assert poster.sent == [], what
        assert run["status"] == "completed" and run["stream"]["outcome"] == "dashboard_only" and run["stream"]["output_text"]

    scenario(check, monkeypatch, tmp_path)


def test_a_delivery_a_restart_resumes_never_goes_to_a_target_changed_meanwhile(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        await add(env.pool, "gossip", sent_at=minutes_ago(env, 5))
        run = await fire(env, sid)
        await left_by_a_dead_server(env, run)
        await env.pool.execute("UPDATE subscription_deliveries SET state = 'pending', attempts = 0 WHERE run_id = $1",
                               run["id"])
        poster.sent.clear()
        # The operator changes where it goes (A to B), and the server restarts: B is not confirmed.
        await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {"notify_target": {"channel": "lark", "chat": GROUP}}})
        scheduler_with(env, poster=poster)
        await env.scheduler.tick()
        await env.scheduler.idle()
        assert poster.sent == []
        assert (await latest(env, sid))["stream"]["outcome"] == "dashboard_only"

    scenario(check, monkeypatch, tmp_path)


def test_a_run_is_pinned_to_the_target_it_started_with_even_when_another_is_confirmed_later(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        await add(env.pool, "gossip", sent_at=minutes_ago(env, 5))

        async def move_and_confirm() -> None:
            await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {"notify_target": {"channel": "lark", "chat": GROUP}}})
            await confirm(env, sid)  # B is confirmed now, but this run began with A
        fake.during = move_and_confirm
        run = await fire(env, sid)
        assert poster.sent == [] and run["stream"]["outcome"] == "dashboard_only"

    scenario(check, monkeypatch, tmp_path)


def test_a_confirmation_names_the_version_it_was_shown_and_a_changed_schedule_refuses_it(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        shown = await version_of(env, sid)  # the panel shows target A
        await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {"notify_target": {"channel": "lark", "chat": GROUP}}})
        status, answer = await env.call("POST", "/api/v1/schedule_admin/confirm_target",
                                        {"schedule_id": sid, "config_version": shown})
        assert status == 409 and "changed" in answer["error"]["message"]
        assert await confirmed_at(env, sid) is None
        status, answer = await env.call("POST", "/api/v1/schedule_admin/confirm_target", {"schedule_id": sid})
        assert status in (400, 409) and "config_version" in answer["error"]["message"]
        assert await confirmed_at(env, sid) is None
        await confirm(env, sid)  # what it is now
        assert await confirmed_at(env, sid) is not None

    scenario(check, monkeypatch, tmp_path)


def test_changing_the_processors_profile_unconfirms_the_target_because_the_confirmation_named_the_old_one(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        await env.pool.execute(
            "INSERT INTO agent_profiles (id, name, runtime_id, model_id) VALUES ('profile-gossip', 'Gossip', "
            "'rt-claude-code', (SELECT id FROM llm_models WHERE provider_id = 'prov-claude-subscription' "
            "AND name = 'sonnet'))")
        sid = await started(env)
        await confirm(env, sid)
        await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {"profile_overrides": {"processor": "profile-gossip"}}})
        await add(env.pool, "gossip", sent_at=minutes_ago(env, 5))
        run = await fire(env, sid)
        assert poster.sent == [] and run["stream"]["outcome"] == "dashboard_only"

    scenario(check, monkeypatch, tmp_path)


def test_the_source_cannot_change_while_a_run_is_open(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await source(env.pool, "other")
        await env.pool.execute("INSERT INTO schedule_runs (id, schedule_id, scheduled_for, trigger, status) "
                               "VALUES ('srun-open', $1, now(), 'manual', 'running')", sid)
        status, answer = await env.call("POST", UPDATE, {"schedule_id": sid, "fields": {"subscription": {"source_id": "other"}}})
        assert status == 409 and "in progress" in answer["error"]["message"]
        assert (await env.pool.fetchval("SELECT source_id FROM subscriptions WHERE schedule_id = $1", sid)) == SOURCE
        await env.pool.execute("UPDATE schedule_runs SET status = 'failed' WHERE id = 'srun-open'")
        await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {"subscription": {"source_id": "other"}}})

    scenario(check, monkeypatch, tmp_path)


def test_a_cursor_reset_waits_for_the_sources_writer_and_refuses_a_time_in_the_future(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        from looper.db import message_stream

        sid = await started(env)
        monkeypatch.setattr(db_subs, "LOCK_WAIT_SECONDS", 0.5)
        async with env.pool.acquire() as writer:  # a pass reading the source's files: it holds the writer lock
            assert await message_stream.try_lock(writer, SOURCE)
            status, answer = await env.call("POST", "/api/v1/schedule_admin/reset_cursor",
                                            {"schedule_id": sid, "after": env.clock.now.isoformat()})
            assert status == 409 and "being read" in answer["error"]["message"]
            await message_stream.unlock(writer, SOURCE)
        # Any future time is refused, to the microsecond; now itself and the second before it are not.
        for ahead, ok in ((timedelta(microseconds=1), False), (timedelta(seconds=1), False), (timedelta(hours=1), False),
                          (timedelta(0), True), (-timedelta(seconds=1), True)):
            status, answer = await env.call("POST", "/api/v1/schedule_admin/reset_cursor",
                                            {"schedule_id": sid, "after": (env.clock.now + ahead).isoformat()})
            assert (status == 200) is ok, (ahead, status, answer)
            if not ok:
                assert status == 400 and "future" in answer["error"]["message"]

    scenario(check, monkeypatch, tmp_path)


def test_a_client_enabling_with_a_profile_change_in_the_same_request_is_refused(monkeypatch, tmp_path):
    async def run(pool):
        await lark_targets(pool)
        await source(pool)
        from looper.db import projects

        project = await projects.create_project(name="Streams", work_dir=str(tmp_path))
        await pool.execute(
            "INSERT INTO agent_profiles (id, name, runtime_id, model_id) VALUES ('profile-gossip', 'Gossip', "
            "'rt-claude-code', (SELECT id FROM llm_models WHERE provider_id = 'prov-claude-subscription' "
            "AND name = 'sonnet'))")
        async with v1_api(tmp_path, pool) as call:
            fields = {"name": "gossip", "cron": "5 * * * *", "timezone": "UTC", "kind": "subscription",
                      "requirement": INSTRUCTIONS, "run_budget_usd": 0.5, "notify_target": {"channel": "lark", "chat": DM},
                      "subscription": SUBSCRIPTION}
            _, created = await call("schedule_admin.create", {"project_id": project["id"], "fields": fields}, client=True)
            sid = created["result"]["schedule"]["schedule_id"]
            status, refused = await call("schedule_admin.update", {"schedule_id": sid, "fields": {
                "enabled": True, "profile_overrides": {"processor": "profile-gossip"}}}, client=True)
            assert status == 400 and "request of its own" in refused["error"]["message"], refused
            assert not await pool.fetchval("SELECT enabled FROM schedules WHERE id = $1", sid)

    run_db(run, monkeypatch, timeout=60, max_size=4)


def test_a_manager_sees_a_subscriptions_state_and_counts_but_never_what_it_made(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        from looper.agents.ops import schedules as ops

        secret = "SECRET-DIGEST-TEXT"
        fake.reply = f"- {secret}"
        sid = await started(env)
        await confirm(env, sid)
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        run = await fire(env, sid)
        assert run["summary"] and secret in run["summary"]  # the dashboard has it
        view = (await env.ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))["schedule"]
        runs = (await env.ok("POST", "/api/v1/schedule_admin/runs", {"schedule_id": sid}))["runs"]
        listed = ops._run_listed(runs[0])
        assert listed["summary"] is None and secret not in str(listed)
        assert listed["status"] == "completed" and listed["cost_usd"] is not None
        assert secret not in str(ops._listed(view)) and secret not in str(ops._run_listed(view["last_run"]))
        await env.pool.execute("UPDATE schedule_runs SET summary = NULL, error = $2 WHERE id = $1", run["id"],
                               f"the push failed: {secret}")
        failed = (await env.ok("POST", "/api/v1/schedule_admin/runs", {"schedule_id": sid}))["runs"][0]
        assert secret not in str(ops._run_listed(failed))

    scenario(check, monkeypatch, tmp_path)


def test_a_run_a_restart_found_in_its_processor_ends_interrupted_and_its_window_is_read_again(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        before = await cursor(env, sid)
        fake.failure = processor.ProcessorFailure.FAILED
        run = await fire(env, sid)
        await env.pool.execute("UPDATE schedule_runs SET status = 'running', finished_at = NULL, "
                               "runner_pid = 999999, runner_started = 'a dead server' WHERE id = $1", run["id"])
        await env.pool.execute("UPDATE subscription_runs SET outcome = 'pending' WHERE run_id = $1", run["id"])
        fake.failure = None
        scheduler_with(env, poster=poster)
        await env.scheduler.tick()
        await env.scheduler.idle()
        ended = await db_schedules.get_run(run["id"])
        assert ended["status"] == "interrupted" and "stopped" in ended["error"] and await cursor(env, sid) == before
        assert poster.sent == []
        # Its window is part of the next run's.
        again = await fire(env, sid)
        assert again["status"] == "completed" and again["stream"]["from_seq"] == before

    scenario(check, monkeypatch, tmp_path)


def test_a_run_of_a_live_server_is_not_settled_by_another(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        fake.failure = processor.ProcessorFailure.FAILED
        run = await fire(env, sid)
        import os
        await env.pool.execute("UPDATE schedule_runs SET status = 'running', finished_at = NULL, runner_pid = $2, "
                               "runner_started = 'another server' WHERE id = $1", run["id"], os.getpid())
        await env.scheduler.tick()
        await env.scheduler.idle()
        assert (await db_schedules.get_run(run["id"]))["status"] == "running"

    scenario(check, monkeypatch, tmp_path)


# --- The operator's word on a push target ---


def test_nothing_is_pushed_before_the_target_is_confirmed_and_the_run_still_works(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        assert await confirmed_at(env, sid) is None  # enabling cleared it (it never was)
        top = await add(env.pool, "gossip", sent_at=minutes_ago(env, 5))
        run = await fire(env, sid)
        assert run["status"] == "completed" and run["stream"]["outcome"] == "dashboard_only"
        assert poster.sent == [] and run["stream"]["output_text"] and await cursor(env, sid) == top
        # The operator sees it on the dashboard; after they confirm, the next window pushes.
        await confirm(env, sid)
        await add(env.pool, "more gossip", sent_at=minutes_ago(env, 5) + timedelta(minutes=10))
        run = await fire(env, sid)
        assert run["stream"]["outcome"] == "delivered" and len(poster.sent) == 1

    scenario(check, monkeypatch, tmp_path)


def test_a_schedule_without_a_target_or_that_never_notifies_keeps_its_output_on_the_dashboard(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        for name, fields in (("no target", {"target": None}), ("never", {"notify": "never"})):
            sid = await started(env, name=name, **fields)
            if fields.get("target", True):
                await confirm(env, sid)
            await add(env.pool, name, sent_at=minutes_ago(env, 5))
            run = await fire(env, sid)
            assert run["stream"]["outcome"] == "dashboard_only", name
        assert poster.sent == []

    scenario(check, monkeypatch, tmp_path)


@pytest.mark.parametrize("change, clears", [
    ({"notify_target": {"channel": "lark", "chat": GROUP}}, True),
    ({"cron": "10 * * * *"}, True), ({"timezone": "Asia/Shanghai"}, True),
    ({"enabled": False}, False), ({"name": "renamed", "requirement": "other instructions"}, False),
    ({"subscription": {"quiet_word": "NONE", "max_events": 100}}, False), ({"notify": "always"}, False)])
def test_what_changes_where_or_when_it_pushes_clears_the_confirmation(monkeypatch, tmp_path, change, clears):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": change})
        assert (await confirmed_at(env, sid) is None) is clears

    scenario(check, monkeypatch, tmp_path)


def test_enabling_again_clears_the_confirmation_and_the_target_can_be_removed(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {"enabled": False}})
        assert await confirmed_at(env, sid) is not None
        await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {"enabled": True}})
        assert await confirmed_at(env, sid) is None
        await confirm(env, sid)
        await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {"notify_target": None}})
        assert await confirmed_at(env, sid) is None
        status, answer = await env.call("POST", "/api/v1/schedule_admin/confirm_target",
                                        {"schedule_id": sid, "config_version": "any"})
        assert status == 400 and "no notify_target" in answer["error"]["message"]

    scenario(check, monkeypatch, tmp_path)


def test_alerts_and_failures_do_not_reach_an_unconfirmed_target_either(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sender = with_sender(env)
        sid = await started(env, alert_failures_after=1)
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        fake.failure = processor.ProcessorFailure.FAILED
        await fire(env, sid)
        assert sender.sent == []
        notes = await env.pool.fetch("SELECT kind, status FROM schedule_notifications WHERE schedule_id = $1", sid)
        assert {(n["kind"], n["status"]) for n in notes} == {("run", "dashboard"), ("failures_alert", "dashboard")}
        await confirm(env, sid)
        await add(env.pool, "y", sent_at=minutes_ago(env, 5) + timedelta(minutes=10))
        await fire(env, sid)
        assert [text for _, _, text in sender.sent if "失败" in text or "连续" in text]  # now it is told

    scenario(check, monkeypatch, tmp_path)


def test_a_group_target_needs_a_bound_chat_and_the_same_confirmation(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        await source(env.pool)
        body = {"kind": "subscription", "requirement": INSTRUCTIONS, "run_budget_usd": 0.5, "subscription": SUBSCRIPTION}
        status, refused = await env.call("POST", CREATE, {"project_id": env.project_id, "fields": {
            "name": "g1", "cron": "*/5 * * * *", **body, "notify_target": {"channel": "lark", "chat": UNBOUND_GROUP}}})
        assert status == 400 and "not one the channel allows" in refused["error"]["message"]
        sid = await started(env, target={"channel": "lark", "chat": GROUP})
        await confirm(env, sid)
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        await fire(env, sid)
        [sent] = poster.sent
        assert sent["target"].kind is TargetKind.CHAT and sent["target"].id == GROUP and sent["target"].group
        assert sent["target"].mention is None  # nobody is @-mentioned in the group

    scenario(check, monkeypatch, tmp_path)


# --- The cursor ---


def test_resetting_the_cursor_to_a_time_reads_exactly_the_events_ingested_after_it(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        moment = env.clock.now
        for name, delta in (("a", -120), ("b", -60), ("c", 60), ("d", 120)):
            await add(env.pool, name, sent_at=minutes_ago(env, 5), ingested_at=moment + timedelta(minutes=delta))
        status, answer = await env.call("POST", "/api/v1/schedule_admin/reset_cursor",
                                        {"schedule_id": sid, "after": moment.isoformat()})
        assert status == 200 and answer["cursor"]["events_after"] == 2, answer
        run = await fire(env, sid)
        assert [line.split(": ", 1)[1] for line in fake.calls[0]["input_text"].splitlines()[1:]] == ["d", "c"]
        assert run["stream"]["events_in_window"] == 2

    scenario(check, monkeypatch, tmp_path)


def test_resetting_after_the_last_event_skips_everything_and_a_running_run_blocks_it(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await add(env.pool, "old", sent_at=minutes_ago(env, 5), ingested_at=env.clock.now - timedelta(hours=1))
        status, answer = await env.call("POST", "/api/v1/schedule_admin/reset_cursor",
                                        {"schedule_id": sid, "after": env.clock.now.isoformat()})
        assert status == 200 and answer["cursor"]["events_after"] == 0
        assert await cursor(env, sid) == answer["cursor"]["committed_seq"]
        await env.pool.execute("INSERT INTO schedule_runs (id, schedule_id, scheduled_for, trigger, status) "
                               "VALUES ('srun-running', $1, now(), 'manual', 'running')", sid)
        status, answer = await env.call("POST", "/api/v1/schedule_admin/reset_cursor",
                                        {"schedule_id": sid, "after": env.clock.now.isoformat()})
        assert status == 409 and "in progress" in answer["error"]["message"]

    scenario(check, monkeypatch, tmp_path)


@pytest.mark.parametrize("after", ["yesterday", "2026-11-19T08:00:00", 5, None])
def test_a_cursor_reset_needs_a_time_with_its_offset(monkeypatch, tmp_path, after):
    async def check(env, poster, fake):
        sid = await started(env)
        status, answer = await env.call("POST", "/api/v1/schedule_admin/reset_cursor",
                                        {"schedule_id": sid, **({"after": after} if after is not None else {})})
        assert status == 400, answer

    scenario(check, monkeypatch, tmp_path)


def test_a_change_of_source_starts_the_cursor_at_the_new_sources_end(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await source(env.pool, "other")
        await add(env.pool, "elsewhere", sent_at=minutes_ago(env, 5), source_id="other")
        await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {"subscription": {"source_id": "other"}}})
        assert await cursor(env, sid) == await env.pool.fetchval("SELECT committed_seq FROM event_sources WHERE id = 'other'")

    scenario(check, monkeypatch, tmp_path)


def test_a_new_subscription_starts_at_the_end_of_its_stream(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        await source(env.pool)
        await add(env.pool, "before it existed", sent_at=minutes_ago(env, 5))
        sid = await started(env)
        assert await cursor(env, sid) == await env.pool.fetchval("SELECT committed_seq FROM event_sources")
        assert (await fire(env, sid))["status"] == "skipped_gate"

    scenario(check, monkeypatch, tmp_path)


# --- The configuration ---


@pytest.mark.parametrize("override, message", [
    ({"subscription": {**SUBSCRIPTION, "source_id": "nowhere"}}, "no event source 'nowhere'"),
    ({"subscription": {**SUBSCRIPTION, "max_events": 5000}}, "declared schema"),
    ({"subscription": {"source_id": "stream"}}, "subscription needs"),
    ({"subscription": None}, "declared schema"),
    ({"overlap": "parallel"}, "cannot overlap"),
    ({"run_budget_usd": None}, "run_budget_usd is required"),
    ({"requirement": " "}, "requirement"),
    ({"command": "echo hi"}, "command does not apply"),
    ({"gate_command": "true"}, "gate_command does not apply"),
    ({"template_key": "t"}, "template_key does not apply"),
    ({"notify_format": "deliverable"}, "report only"),
    ({"profile_overrides": {"worker": "x"}}, "processor of its processor only|processor only"),
    ({"cron": "not a rule"}, "5 fields")])
def test_a_subscription_the_api_cannot_use_is_refused_with_the_reason(monkeypatch, tmp_path, override, message):
    async def check(env, poster, fake):
        await source(env.pool)
        fields = {"name": "bad", "cron": "*/5 * * * *", "timezone": "UTC", "kind": "subscription",
                  "requirement": INSTRUCTIONS, "run_budget_usd": 0.5, "subscription": SUBSCRIPTION, **override}
        status, answer = await env.call("POST", CREATE, {"project_id": env.project_id, "fields": fields})
        assert status == 400, answer
        import re
        assert re.search(message, answer["error"]["message"]), answer
        assert await env.pool.fetchval("SELECT count(*) FROM schedules") == 0

    scenario(check, monkeypatch, tmp_path)


def test_a_subscription_field_on_another_kind_or_none_on_a_subscription_is_refused(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        await source(env.pool)
        fields = {"name": "x", "cron": "*/5 * * * *", "timezone": "UTC", "requirement": "r"}
        status, answer = await env.call("POST", CREATE, {"project_id": env.project_id, "fields": {
            **fields, "subscription": SUBSCRIPTION}})
        assert status == 400 and "kind subscription only" in answer["error"]["message"]
        status, answer = await env.call("POST", CREATE, {"project_id": env.project_id, "fields": {
            **fields, "kind": "subscription", "run_budget_usd": 1}})
        assert status == 400 and "needs its subscription" in answer["error"]["message"]
        agent = (await env.schedule())["schedule_id"]
        status, answer = await env.call("POST", UPDATE, {"schedule_id": agent, "fields": {"subscription": {"quiet_word": "A"}}})
        assert status == 400 and "kind subscription only" in answer["error"]["message"]
        status, answer = await env.call("POST", UPDATE, {"schedule_id": agent, "fields": {
            "profile_overrides": {"processor": "claude-code/opus"}}})
        assert status == 400 and "subscriptions only" in answer["error"]["message"]

    scenario(check, monkeypatch, tmp_path)


def test_a_subscription_reads_back_with_its_stream_its_runs_and_the_confirmation_text(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        run = await fire(env, sid)
        view = (await env.ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))["schedule"]
        assert view["kind"] == "subscription" and view["subscription"]["quiet_word"] == "SKIP"
        assert view["subscription"]["target_confirmed_at"] is None and "确认推送目标" in view["subscription"]["confirmation"]
        runs = (await env.ok("POST", "/api/v1/schedule_admin/runs", {"schedule_id": sid}))["runs"]
        assert runs[0]["run_id"] == run["id"] and runs[0]["subscription"]["outcome"] == "dashboard_only"
        assert runs[0]["subscription"]["events_selected"] == 1 and runs[0]["kind"] == "subscription"
        listed = (await env.ok("POST", "/api/v1/schedule_admin/list", {}))["schedules"]
        assert [s["subscription"]["max_events"] for s in listed if s["schedule_id"] == sid] == [500]

    scenario(check, monkeypatch, tmp_path)


def test_a_subscription_creates_no_task_and_needs_no_template_for_a_templates_only_owner(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        await env.pool.execute("UPDATE managers SET task_policy = 'templates_only' WHERE id = 'manager'")
        sid = await started(env)
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        run = await fire(env, sid)
        assert run["status"] == "completed" and run["task_id"] is None
        assert await env.pool.fetchval("SELECT count(*) FROM tasks WHERE schedule_id = $1", sid) == 0

    scenario(check, monkeypatch, tmp_path)


def test_the_processors_profile_follows_the_schedules_choice(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        await env.pool.execute(
            "INSERT INTO agent_profiles (id, name, runtime_id, model_id) VALUES ('profile-gossip', 'Gossip', "
            "'rt-claude-code', (SELECT id FROM llm_models WHERE provider_id = 'prov-claude-subscription' "
            "AND name = 'sonnet'))")
        sid = await started(env)
        before = await processor.chosen_profile(env.project_id, sid)
        view = (await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {
            "profile_overrides": {"processor": "profile-gossip"}}}))["schedule"]
        assert view["profile_overrides"] == {"processor": "profile-gossip"}
        after = await processor.chosen_profile(env.project_id, sid)
        assert after["profile_id"] == "profile-gossip" and after["profile_id"] != before["profile_id"]
        await confirm(env, sid)
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        await fire(env, sid)
        assert fake.calls[0]["schedule_id"] == sid and fake.calls[0]["project_id"] == env.project_id

    scenario(check, monkeypatch, tmp_path)


def test_a_subscription_with_a_processor_that_cannot_run_is_not_enabled(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        async def cannot(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"problem": "its runtime has no mode without tools"}
        monkeypatch.setattr(processor, "chosen_profile", cannot)
        sid = await started(env, enable=False)
        status, answer = await env.call("POST", UPDATE, {"schedule_id": sid, "fields": {"enabled": True}})
        assert status == 409 and "cannot run" in answer["error"]["message"]
        assert not await env.pool.fetchval("SELECT enabled FROM schedules WHERE id = $1", sid)

    scenario(check, monkeypatch, tmp_path)


# --- Through the client API: the confirmation the operator gives ---


def test_a_client_enabling_a_subscription_is_shown_what_it_will_do_and_confirms_the_target(monkeypatch, tmp_path):
    class Hooks:
        source = "/Library/managed.json"
        events = ("UserPromptSubmit",)

        def view(self):
            return {"source": self.source, "events": list(self.events), "readable": True}

    async def run(pool):
        await lark_targets(pool)
        monkeypatch.setattr(processor, "_managed_hooks", lambda settings: Hooks())
        await source(pool)
        from looper.db import projects

        project = await projects.create_project(name="Streams", work_dir=str(tmp_path))
        async with v1_api(tmp_path, pool) as call:
            fields = {"name": "gossip", "cron": "5 * * * *", "timezone": "America/Los_Angeles", "kind": "subscription",
                      "requirement": INSTRUCTIONS, "run_budget_usd": 0.5, "daily_budget_usd": 5,
                      "notify_target": {"channel": "lark", "chat": DM}, "subscription": SUBSCRIPTION}
            status, created = await call("schedule_admin.create", {"project_id": project["id"], "fields": fields},
                                         client=True)
            assert status == 200, created
            sid = created["result"]["schedule"]["schedule_id"]
            status, pending = await call("schedule_admin.update", {"schedule_id": sid, "fields": {"enabled": True}},
                                         client=True)
            assert status == 202 and pending["status"] == "needs_confirmation", pending
            text = pending["impact"]
            for fact in (f"推送目标：Lark · {DM}", "（私聊）", "机器人身份：", "channel-lark", "每小时第 5 分钟（America/Los_Angeles）",
                         "接下来 3 次：", "每天最多推送：24 次", "处理 Profile：", "来源：", "processor.chosen_profile",
                         "本机有企业管理的 hooks，会看到处理的消息（UserPromptSubmit）", "单次 $0.5；每日 $5"):
                assert fact in text, (fact, text)
            assert text.count("-") >= 7
            assert await pool.fetchval("SELECT enabled FROM schedules WHERE id = $1", sid) is False  # nothing yet
            status, done = await call("/api/v1/confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
            assert status == 200, done
            row = await pool.fetchrow("SELECT s.enabled, u.target_confirmed_at FROM schedules s "
                                      "JOIN subscriptions u ON u.schedule_id = s.id WHERE s.id = $1", sid)
            assert row["enabled"] and row["target_confirmed_at"] is not None
            # A rename needs no word; a new target does, and confirming it confirms it.
            status, _ = await call("schedule_admin.update", {"schedule_id": sid, "fields": {"name": "renamed"}}, client=True)
            assert status == 200
            status, pending = await call("schedule_admin.update", {"schedule_id": sid, "fields": {
                "notify_target": {"channel": "lark", "chat": GROUP}}}, client=True)
            assert status == 202 and f"Lark · {GROUP}" in pending["impact"] and "（群聊）" in pending["impact"]
            assert (await pool.fetchval("SELECT notify_chat FROM schedules WHERE id = $1", sid)) == DM
            await call("/api/v1/confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
            row = await pool.fetchrow("SELECT s.notify_chat, u.target_confirmed_at FROM schedules s "
                                      "JOIN subscriptions u ON u.schedule_id = s.id WHERE s.id = $1", sid)
            assert row["notify_chat"] == GROUP and row["target_confirmed_at"] is not None

    run_db(run, monkeypatch, timeout=60, max_size=4)


def test_a_client_can_confirm_the_target_as_it_is_and_reset_the_cursor_after_confirming_it_too(monkeypatch, tmp_path):
    async def run(pool):
        await lark_targets(pool)
        await source(pool)
        from looper.db import projects

        project = await projects.create_project(name="Streams", work_dir=str(tmp_path))
        async with v1_api(tmp_path, pool) as call:
            fields = {"name": "gossip", "cron": "5 * * * *", "timezone": "UTC", "kind": "subscription",
                      "requirement": INSTRUCTIONS, "run_budget_usd": 0.5, "notify_target": {"channel": "lark", "chat": DM},
                      "subscription": SUBSCRIPTION}
            _, created = await call("schedule_admin.create", {"project_id": project["id"], "fields": fields}, client=True)
            sid = created["result"]["schedule"]["schedule_id"]
            status, pending = await call("schedule_admin.confirm_target", {"schedule_id": sid}, client=True)
            assert status == 202 and "确认推送目标" in pending["impact"] and "推送目标：" in pending["impact"]
            assert await pool.fetchval("SELECT target_confirmed_at FROM subscriptions WHERE schedule_id = $1", sid) is None
            status, done = await call("/api/v1/confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
            assert status == 200 and done["result"]["schedule"]["subscription"]["target_confirmed_at"]
            when = "2026-01-01T00:00:00-08:00"
            status, pending = await call("schedule_admin.reset_cursor", {"schedule_id": sid, "after": when}, client=True)
            assert status == 202 and "Reset the cursor" in pending["impact"] and "2026-01-01T08:00:00+00:00" in pending["impact"]
            status, done = await call("/api/v1/confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
            assert status == 200 and done["result"]["cursor"]["events_after"] == 0
            status, refused = await call("schedule_admin.confirm_target", {"schedule_id": sid + "x"}, client=True)
            assert status == 404

    run_db(run, monkeypatch, timeout=60, max_size=4)


def test_a_confirmation_does_not_apply_to_a_schedule_that_changed_after_it_was_shown(monkeypatch, tmp_path):
    async def run(pool):
        await lark_targets(pool)
        await source(pool)
        from looper.db import projects

        project = await projects.create_project(name="Streams", work_dir=str(tmp_path))
        async with v1_api(tmp_path, pool) as call:
            fields = {"name": "gossip", "cron": "5 * * * *", "timezone": "UTC", "kind": "subscription",
                      "requirement": INSTRUCTIONS, "run_budget_usd": 0.5, "notify_target": {"channel": "lark", "chat": DM},
                      "subscription": SUBSCRIPTION}
            _, created = await call("schedule_admin.create", {"project_id": project["id"], "fields": fields}, client=True)
            sid = created["result"]["schedule"]["schedule_id"]
            status, pending = await call("schedule_admin.confirm_target", {"schedule_id": sid}, client=True)
            assert status == 202
            await call("schedule_admin.update", {"schedule_id": sid, "fields": {"subscription": {"max_events": 7}}})
            status, refused = await call("/api/v1/confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
            assert status == 409 and "changed" in refused["error"]["message"]
            assert await pool.fetchval("SELECT target_confirmed_at FROM subscriptions WHERE schedule_id = $1", sid) is None

    run_db(run, monkeypatch, timeout=60, max_size=4)


# --- A lost answer is not a refusal ---


@pytest.mark.parametrize("receipt", [PostReceipt(False, error="timed out"),
                                     PostReceipt(False, error="no answer", outcome=PostOutcome.UNKNOWN)])
def test_a_post_that_may_have_been_accepted_is_uncertain_and_not_asked_again(monkeypatch, tmp_path, receipt):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        top = await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        calls = 0

        async def lost(*args: Any, **kwargs: Any) -> PostReceipt:
            nonlocal calls
            calls += 1
            return receipt
        scheduler_with(env, poster=lost)
        run = await fire(env, sid)
        assert calls == 1 and run["status"] == "completed" and run["stream"]["outcome"] == "uncertain"
        assert await cursor(env, sid) == top

    scenario(check, monkeypatch, tmp_path)


def _lark_poster(adapter):
    async def post(channel_id, target, text, *, idempotency_key=None):
        return await adapter.post(target, text, idempotency_key=idempotency_key)
    return post


@pytest.mark.parametrize("answer, outcome, asks", [
    ("lost", "uncertain", 1),  # lark-cli took the message and its answer could not be read
    ("refused", "failed", 3),  # the platform said no: asked again, three asks in all
])
def test_the_lark_adapter_tells_a_refusal_from_a_lost_answer(
        monkeypatch, tmp_path, lark_cli_stand_in, answer, outcome, asks):  # noqa: F811
    import shutil

    from looper.agents.channels.allowlist import SenderAllowlist
    from looper.agents.channels.lark import LarkChannelAdapter

    executable = Path(f"{shutil.which('lark-cli')}.py")
    if answer == "lost":
        executable.write_text(executable.read_text().replace("print(json.dumps(value))", "print('', end='')"))
    else:
        executable.write_text(executable.read_text().replace(
            "if args[:4] == [\"im\", \"+messages-send\", \"--as\", \"bot\"]:",
            "if args[:4] == [\"im\", \"+messages-send\", \"--as\", \"bot\"]:\n    refuse(\"Bot can NOT be out of the chat\", api_code=230002)"))

    async def check(env, poster, fake):
        adapter = LarkChannelAdapter(runtime_root=tmp_path, allowlist=SenderAllowlist("channel-lark", frozenset({DM})))
        scheduler_with(env, poster=_lark_poster(adapter))
        sid = await started(env)
        await confirm(env, sid)
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        run = await fire(env, sid)
        sends = [line for line in lark_cli_stand_in.read_text().splitlines() if "+messages-send" in line]
        assert len(sends) == asks, sends
        assert run["stream"]["outcome"] == outcome
        await adapter.stop()

    scenario(check, monkeypatch, tmp_path)


@pytest.mark.parametrize("failure, outcome, asks", [("timeout", "uncertain", 1), ("bad request", "failed", 3)])
def test_the_telegram_adapter_tells_a_refusal_from_a_lost_answer(monkeypatch, tmp_path, failure, outcome, asks):
    from types import SimpleNamespace

    from telegram.error import BadRequest, TimedOut

    from looper.agents.channels.allowlist import SenderAllowlist
    from looper.agents.channels.telegram import TelegramChannelAdapter
    from tests.schedule_support import TELEGRAM_CHAT, allow_telegram

    sent = []

    async def send_message(**kwargs):
        sent.append(kwargs)  # the request went out
        raise TimedOut("read timed out") if failure == "timeout" else BadRequest("chat not found")

    async def check(env, poster, fake):
        await allow_telegram(env)
        adapter = TelegramChannelAdapter(bot_token="0:synthetic",
                                         allowlist=SenderAllowlist("channel-telegram", frozenset({TELEGRAM_CHAT})))
        adapter._app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))

        async def post(channel_id, target, text, *, idempotency_key=None):
            return await adapter.post(target, text, idempotency_key=idempotency_key)
        scheduler_with(env, poster=post)
        sid = await started(env, target={"channel": "telegram", "chat": TELEGRAM_CHAT})
        await confirm(env, sid)
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        run = await fire(env, sid)
        assert len(sent) == asks and run["stream"]["outcome"] == outcome

    scenario(check, monkeypatch, tmp_path)


# --- What a notice about a subscription says and where it goes ---


def test_a_failure_notice_goes_through_the_same_check_and_says_nothing_of_the_output(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sender = with_sender(env)
        secret = "SECRET-PROCESSOR-DETAIL"
        sid = await started(env, alert_failures_after=1)
        await confirm(env, sid)
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        fake.failure = processor.ProcessorFailure.FAILED

        async def move() -> None:  # the target moves to B, and B is confirmed, while the run (started for A) is going
            await env.ok("POST", UPDATE, {"schedule_id": sid, "fields": {"notify_target": {"channel": "lark", "chat": GROUP}}})
            await confirm(env, sid)
        fake.during = move
        await fire(env, sid)
        assert sender.sent == []  # neither the failure notice nor the alert: the run did not start for B
        notes = await env.pool.fetch("SELECT kind, status, text FROM schedule_notifications WHERE schedule_id = $1", sid)
        assert {n["status"] for n in notes} == {"dashboard"}
        fake.during = None
        fake.failure = processor.ProcessorFailure.FAILED
        await add(env.pool, "y", sent_at=minutes_ago(env, 5) + timedelta(minutes=10))
        await env.pool.execute("UPDATE schedule_runs SET error = $2 WHERE schedule_id = $1 AND status = 'failed'", sid, secret)
        await fire(env, sid)
        sent = [text for _, _, text in sender.sent]
        assert sent and all(secret not in text and "synthetic failure" not in text for text in sent)
        assert any("详情见 Dashboard" in text for text in sent)

    scenario(check, monkeypatch, tmp_path)


def test_a_run_whose_processor_launched_with_another_profile_than_the_confirmed_one_is_not_pushed(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        profile = (await processor.chosen_profile(env.project_id, sid))["profile_id"]
        original = fake.__call__

        async def launched_after_an_edit(**kwargs):
            # The Profile is edited in place and the run launches with it: the facts it reports are the new ones.
            await env.pool.execute("UPDATE agent_profiles SET effort = 'high' WHERE id = $1", profile)
            run = await original(**kwargs)
            return processor.ProcessorRun(run.reply, run.record_id, run.cost_usd,
                                          profile=await processor.run_facts(env.project_id, sid))
        monkeypatch.setattr(processor, "run", launched_after_an_edit)
        run = await fire(env, sid)
        assert poster.sent == [] and run["stream"]["outcome"] == "dashboard_only"

    scenario(check, monkeypatch, tmp_path)


def test_an_in_place_edit_of_the_processors_profile_unconfirms_the_target(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        before = await version_of(env, sid)
        profile = (await processor.chosen_profile(env.project_id, sid))["profile_id"]
        await env.pool.execute("UPDATE agent_profiles SET effort = 'high' WHERE id = $1", profile)
        assert await version_of(env, sid) != before  # the runtime, model, provider and effort are bound, not the id
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))
        run = await fire(env, sid)
        assert poster.sent == [] and run["stream"]["outcome"] == "dashboard_only"

    scenario(check, monkeypatch, tmp_path)
