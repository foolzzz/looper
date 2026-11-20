"""Subscription rules on a real PostgreSQL: a digest-like subscription (direct messages, @-mentions and keyword rules
over listed groups) selects, tags, counts and pushes; what must never be quiet is listed by code; private content
reaches only its operator's own chat. Runs only with a disposable LOOPER_TEST_DATABASE_URL."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from looper.agents import processor
from tests.db_support import DSN, run_db, v1_api
from tests.subscription_support import (DM, GROUP, INSTRUCTIONS, SOURCE, UPDATE, add, confirm,
                                        confirmed_at, fire, lark_targets, minutes_ago, scenario, source, started)

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
ME = "ou_operatorsynthetic_self"
HEADER = "🔔 速报 · 直接找你 {count:直接} / 告警 {count:告警} / 共 {count}"
RULES = [
    {"name": "直接", "label": "单聊", "chat_kinds": ["p2p"], "sender_kinds": ["user"], "exclude_self": True,
     "never_quiet": True},
    {"name": "直接", "label": "@我", "mentions_me": "only", "never_quiet": True},
    {"name": "告警", "include_chats": ["oc_alerts"], "text_match": "告警|故障", "areas": {"oc_alerts": "存储"}},
]
DIGEST = {"source_id": SOURCE, "render": "lines_v2", "rules": RULES, "max_age_minutes": 120, "max_events": 300,
          "text_max_chars": 300, "quiet_word": "SKIP", "output_header": HEADER}


async def digest(env, **fields: Any) -> str:
    await source(env.pool)
    await env.pool.execute("UPDATE event_sources SET user_open_id = $1 WHERE id = $2", ME, SOURCE)
    return await started(env, subscription=DIGEST, **fields)


async def traffic(env) -> None:
    now = lambda m: minutes_ago(env, m)  # noqa: E731
    await add(env.pool, "能帮我看一下吗", chat_id="oc_dm", kind="p2p", sent_at=now(9), sender="ou_alice", chat_name="Alice")
    await add(env.pool, "my own reply", chat_id="oc_dm", kind="p2p", sent_at=now(8), sender=ME, chat_name="Alice")
    await add(env.pool, "build done", chat_id="oc_dm", kind="p2p", sent_at=now(7), sender="cli_bot", sender_kind="app",
              chat_name="Alice")
    await add(env.pool, "@me please decide", chat_id="oc_lounge", sent_at=now(6), mentions_me=True, chat_name="Lounge")
    await add(env.pool, "磁盘告警 90%", chat_id="oc_alerts", sent_at=now(5), chat_name="Storage")
    await add(env.pool, "all quiet here", chat_id="oc_alerts", sent_at=now(4), chat_name="Storage")
    await add(env.pool, "告警 in an unlisted chat", chat_id="oc_other", sent_at=now(3), chat_name="Other")


def test_a_digest_like_subscription_selects_by_rules_tags_the_lines_and_counts_in_its_header(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await digest(env)
        await confirm(env, sid)
        await traffic(env)
        run = await fire(env, sid)
        assert run["status"] == "completed" and run["stream"]["outcome"] == "delivered"
        [call] = fake.calls
        lines = call["input_text"].splitlines()
        assert lines[0] == "消息(共 3 条):"
        tails = [line.split("] ", 1)[1] for line in lines[1:]]
        assert tails == ["【告警·存储】《Storage》 ou_alice…".replace("ou_alice…", tails[0].split(" ", 1)[1]),
                         "【@我】《Lounge》 " + tails[1].split(" ", 1)[1], "【单聊】《Alice》 " + tails[2].split(" ", 1)[1]]
        assert tails[0].endswith("磁盘告警 90%") and tails[1].endswith("@me please decide") and tails[2].endswith("能帮我看一下吗")
        [sent] = poster.sent
        assert sent["text"].startswith("🔔 速报 · 直接找你 2 / 告警 1 / 共 3\n\n")
        assert (run["stream"]["events_selected"], run["stream"]["events_in_window"]) == (3, 7)

    scenario(check, monkeypatch, tmp_path)


def test_a_rule_that_must_never_be_quiet_pushes_its_messages_when_the_processor_says_skip(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        fake.reply = "SKIP"
        sid = await digest(env)
        await confirm(env, sid)
        await traffic(env)
        top = await env.pool.fetchval("SELECT max(seq) FROM source_events")
        run = await fire(env, sid)
        assert run["status"] == "completed" and run["stream"]["outcome"] == "delivered"
        [sent] = poster.sent
        text = sent["text"]
        assert text.startswith("🔔 速报 · 直接找你 2 / 告警 1 / 共 3\n\n")
        assert "单聊\n- **Alice**：能帮我看一下吗" in text and "@我\n- **Lounge**：@me please decide" in text
        assert "磁盘告警" not in text  # the alert rule is not never_quiet: the processor judged it quiet
        assert sent["target"].id == DM and sent["key"] == run["id"]
        assert await env.pool.fetchval("SELECT cursor_seq FROM subscriptions WHERE schedule_id = $1", sid) == top

    scenario(check, monkeypatch, tmp_path)


def test_without_a_never_quiet_message_the_skip_stays_quiet_and_the_fallback_loses_its_at_marks(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        fake.reply = "SKIP"
        sid = await digest(env)
        await confirm(env, sid)
        await add(env.pool, "磁盘告警", chat_id="oc_alerts", sent_at=minutes_ago(env, 5))
        run = await fire(env, sid)
        assert run["stream"]["outcome"] == "quiet" and poster.sent == []
        await add(env.pool, "@所有人 <at user_id=\"ou_x\">小明</at> look", chat_id="oc_lounge", mentions_me=True,
                  sent_at=minutes_ago(env, 2))
        run = await fire(env, sid)
        [sent] = poster.sent
        assert "@所有人" not in sent["text"] and "<at" not in sent["text"] and "所有人 小明 look" in sent["text"]

    scenario(check, monkeypatch, tmp_path)


def test_the_fallback_is_split_and_delivered_like_the_processors_output(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        fake.reply = "SKIP"
        sid = await digest(env)
        await confirm(env, sid)
        for n in range(30):
            await add(env.pool, f"{n} " + "说" * 190, chat_id="oc_dm", kind="p2p", sent_at=minutes_ago(env, 100) + timedelta(seconds=n),
                      sender="ou_alice", chat_name="Alice")
        run = await fire(env, sid)
        assert len(poster.sent) == 2 and [m["key"] for m in poster.sent] == [run["id"], f"{run['id']}-2"]
        assert all(len(m["text"]) <= 4000 for m in poster.sent)
        deliveries = await env.pool.fetch("SELECT state FROM subscription_deliveries WHERE run_id = $1", run["id"])
        assert [d["state"] for d in deliveries] == ["sent", "sent"]

    scenario(check, monkeypatch, tmp_path)


def test_a_subscription_without_rules_reads_exactly_as_before(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env)
        await confirm(env, sid)
        await add(env.pool, "plain", sent_at=minutes_ago(env, 5), chat_id="oc_a")
        await fire(env, sid)
        assert fake.calls[0]["input_text"].splitlines()[1].split("]", 1)[1].startswith("《Ops room》 ")  # lines_v1: no tag
        assert poster.sent[0]["text"].startswith("🍉 吃瓜速报 · 过去1小时\n\n")
        assert await env.pool.fetchval("SELECT count(*) FROM subscription_daily_claims WHERE schedule_id = $1", sid) == 0

    scenario(check, monkeypatch, tmp_path)


# --- Private content reaches only its operator ---


@pytest.mark.parametrize("rule", [
    {"name": "d", "chat_kinds": ["p2p"]}, {"name": "m", "chat_kinds": ["group"], "mentions_me": "only"},
    {"name": "n", "chat_kinds": ["group"], "never_quiet": True}], ids=["p2p", "mentions-only", "never-quiet"])
def test_a_private_subscription_cannot_target_a_group_at_create_update_or_confirm(monkeypatch, tmp_path, rule):
    async def check(env, poster, fake):
        await source(env.pool)
        sub = {**DIGEST, "rules": [rule]}
        fields = {"name": "g", "cron": "5 * * * *", "kind": "subscription", "requirement": INSTRUCTIONS,
                  "run_budget_usd": 0.5, "subscription": sub}
        status, answer = await env.call("POST", "/api/v1/schedule_admin/create", {"project_id": env.project_id, "fields": {
            **fields, "notify_target": {"channel": "lark", "chat": GROUP}}})
        assert status == 400 and "only to your own direct chat" in answer["error"]["message"]
        sid = await started(env, name="dm", subscription=sub)  # a direct chat is fine
        status, answer = await env.call("POST", UPDATE, {"schedule_id": sid, "fields": {
            "notify_target": {"channel": "lark", "chat": GROUP}}})
        assert status == 400 and "only to your own direct chat" in answer["error"]["message"]
        assert (await env.pool.fetchval("SELECT notify_chat FROM schedules WHERE id = $1", sid)) == DM
        # A subscription that becomes private while it pushes to a group is refused too.
        calm = await started(env, name="calm", subscription={**sub, "rules": [{"name": "c", "chat_kinds": ["group"]}]},
                             target={"channel": "lark", "chat": GROUP})
        status, answer = await env.call("POST", UPDATE, {"schedule_id": calm, "fields": {"subscription": {"rules": [rule]}}})
        assert status == 400 and "only to your own direct chat" in answer["error"]["message"]
        # Forced into the database, it still cannot be confirmed.
        await env.pool.execute("UPDATE subscriptions SET rules = $2::jsonb WHERE schedule_id = $1", calm,
                               __import__("json").dumps([{**{"label": "", "chat_kinds": ["group"], "include_chats": [],
                                                              "exclude_chats": [], "sender_kinds": ["user"], "exclude_self": False,
                                                              "mentions_me": "any", "text_match": "", "never_quiet": False,
                                                              "areas": {}}, **rule}]))
        status, answer = await env.call("POST", "/api/v1/schedule_admin/confirm_target", {
            "schedule_id": calm, "config_version": "x"})
        assert status == 400 and "only to your own direct chat" in answer["error"]["message"]
        assert await confirmed_at(env, calm) is None

    scenario(check, monkeypatch, tmp_path)


def test_a_group_may_still_get_a_subscription_that_selects_nothing_private(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env, subscription={**DIGEST, "rules": [{"name": "w", "chat_kinds": ["group"], "mentions_me": "exclude",
                                                                    "text_match": "告警"}]},
                            target={"channel": "lark", "chat": GROUP})
        await confirm(env, sid)

    scenario(check, monkeypatch, tmp_path)


def test_a_run_pinned_to_a_group_before_its_subscription_became_private_pushes_nothing(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await started(env, subscription={**DIGEST, "rules": [{"name": "w", "chat_kinds": ["group"]}]},
                            target={"channel": "lark", "chat": GROUP})
        await confirm(env, sid)
        await add(env.pool, "x", sent_at=minutes_ago(env, 5))

        async def become_private() -> None:
            await env.pool.execute("UPDATE subscriptions SET rules = jsonb_set(rules, '{0,never_quiet}', 'true') "
                                   "WHERE schedule_id = $1", sid)
        fake.during = become_private
        run = await fire(env, sid)
        assert poster.sent == [] and run["stream"]["outcome"] == "dashboard_only"

    scenario(check, monkeypatch, tmp_path)


def test_the_confirmation_says_the_subscription_includes_private_messages_and_binds_nothing_new(monkeypatch, tmp_path):
    async def check(env, poster, fake):
        sid = await digest(env)
        view = (await env.ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))["schedule"]
        assert "私密内容" in view["subscription"]["confirmation"] and "你自己的私聊" in view["subscription"]["confirmation"]
        assert view["subscription"]["rules"][0]["name"] == "直接" and view["subscription"]["render"] == "lines_v2"
        plain = await started(env, name="plain")
        view = (await env.ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": plain}))["schedule"]
        assert "私密内容" not in view["subscription"]["confirmation"]

    scenario(check, monkeypatch, tmp_path)


@pytest.mark.parametrize("change, message", [
    ({"rules": [{"name": "r", "text_match": "(a+)+$"}]}, "repeats a group"),
    ({"rules": [{"name": "r", "text_match": "["}]}, "not a valid pattern"),
    ({"output_header": "{count:x} {bad}"}, "braces")])
def test_the_api_refuses_a_rule_or_header_it_cannot_use(monkeypatch, tmp_path, change, message):
    async def check(env, poster, fake):
        await source(env.pool)
        sid = await started(env)
        status, answer = await env.call("POST", UPDATE, {"schedule_id": sid, "fields": {"subscription": change}})
        assert status == 400, answer
        assert message in str(answer)
        status, answer = await env.call("POST", "/api/v1/schedule_admin/create", {"project_id": env.project_id, "fields": {
            "name": "n", "cron": "5 * * * *", "kind": "subscription", "requirement": INSTRUCTIONS, "run_budget_usd": 0.5,
            "subscription": {**DIGEST, **change}}})
        assert status == 400 and message in str(answer)

    scenario(check, monkeypatch, tmp_path)


def test_a_client_is_shown_the_private_note_when_it_enables_a_digest_like_subscription(monkeypatch, tmp_path):
    async def run(pool):
        await lark_targets(pool)
        await source(pool)
        from looper.db import projects

        project = await projects.create_project(name="Streams", work_dir=str(tmp_path))
        async with v1_api(tmp_path, pool) as call:
            fields = {"name": "digest", "cron": "5 * * * *", "timezone": "UTC", "kind": "subscription",
                      "requirement": INSTRUCTIONS, "run_budget_usd": 0.5, "notify_target": {"channel": "lark", "chat": DM},
                      "subscription": DIGEST}
            _, created = await call("schedule_admin.create", {"project_id": project["id"], "fields": fields}, client=True)
            sid = created["result"]["schedule"]["schedule_id"]
            status, pending = await call("schedule_admin.update", {"schedule_id": sid, "fields": {"enabled": True}}, client=True)
            assert status == 202 and "私密内容" in pending["impact"]
            status, refused = await call("schedule_admin.update", {"schedule_id": sid, "fields": {
                "notify_target": {"channel": "lark", "chat": GROUP}}}, client=True)
            assert status == 400 and "only to your own direct chat" in refused["error"]["message"]

    run_db(run, monkeypatch, timeout=60, max_size=4)


def test_a_database_without_the_rules_column_gets_it_on_upgrade(monkeypatch):
    from looper.db import connection

    async def check(pool):
        await pool.execute("ALTER TABLE subscriptions DROP COLUMN rules; ALTER TABLE subscriptions DROP CONSTRAINT "
                           "subscriptions_render_check, ADD CONSTRAINT subscriptions_render_check CHECK (render IN ('lines_v1')); "
                           "DELETE FROM schema_migrations WHERE name = 'subscription_rules'")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert await pool.fetchval("SELECT rules FROM subscriptions LIMIT 1") is None  # no row; the column answers
        await pool.execute("INSERT INTO event_sources (id, kind, name) VALUES ('s', 'lark_user', 'S')")
        await pool.execute("INSERT INTO projects (id, name) VALUES ('p', 'P')")
        await pool.execute("INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, kind) "
                           "VALUES ('x', 'p', 'N', '5 * * * *', 'UTC', 'r', 'subscription')")
        await pool.execute("INSERT INTO subscriptions (schedule_id, source_id, chat_kinds, sender_kinds, max_age_minutes, "
                           "max_events, text_max_chars, render) VALUES ('x', 's', '{group}', '{user}', 60, 10, 200, 'lines_v2')")
        assert await pool.fetchval("SELECT rules::text FROM subscriptions") == "[]"

    run_db(check, monkeypatch, timeout=30)


def test_the_processor_run_it_is_given_needs_no_new_handling_for_a_blank_batch():
    assert processor.is_quiet("**SKIP**", "SKIP")  # the quiet word the fallback is judged against is the same one
