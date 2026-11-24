"""The "needs you" summary (``agents.attention``): each section read from its own source, what is left out, a source
that cannot be read, and the API that serves it."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import asyncpg
import pytest

from looper.agents import api_v1, attention
from looper.db import connection, ledger, schedules as db_schedules
from tests import ledger_support as sup, leads_support as leads, subscription_support as stream
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def no_status_reader(monkeypatch):
    monkeypatch.setattr(attention, "_status_checks", None)  # what a serve elsewhere in this process may have set
    monkeypatch.setattr(attention, "_catchup_checks", None, raising=False)


def section(summary, kind):
    return next((s for s in summary["sections"] if s["kind"] == kind), None)


def titles(summary, kind):
    return [item["title"] for item in section(summary, kind)["items"]]


async def setting(pool, key, value="true"):
    await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, $2) "
                       "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", key, value)


@pytest.mark.parametrize("state", ["uncertain", "failed", "missed"])
def test_terminal_reminder_waits_once_for_the_user_without_an_idle_push(monkeypatch, state):
    from looper.agents import idle_checks
    from looper.db import reminders

    async def check(pool):
        row = await reminders.create(text="Check the application", due_at=NOW - timedelta(days=5), timezone="UTC",
                                     channel_id="channel-lark", chat="ou_synthetic", created_channel="web",
                                     created_message_id=None)
        await reminders.settle(pool, row["id"], state, now=NOW - timedelta(days=4), from_status="pending")
        for _ in range(2):
            summary = await attention.collect()
            found = section(summary, "reminders")
            assert found is not None and found["count"] == 1
            item, = found["items"]
            assert item["title"] == row["text"]
            assert item["facts"]["state"] == state and item["facts"]["due_at"] == row["due_at"]
            assert item["key"] == f"reminder:{row['id']}"
            assert await attention.earliest("reminders") == item
            text = attention.summary_text(summary)
            assert text.count(row["text"]) == 1 and state in text
            assert row["due_at"].strftime("%Y-%m-%d %H:%M") in text
            assert f"取消提醒 #{row['id']}" in text
            if state == "uncertain":
                assert "可能未送达" in text
            assert idle_checks.select(summary, now=NOW) == []
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 0
        _, changed = await reminders.cancel(row["id"], now=NOW)
        assert changed
        assert section(await attention.collect(), "reminders")["count"] == 0
        assert await attention.earliest("reminders") is None

    run_db(check, monkeypatch)


def test_terminal_reminder_visibility_expires_seven_days_after_settlement(monkeypatch):
    from looper.db import reminders

    async def check(pool):
        for label, settled in (("expired", NOW - timedelta(days=7)),
                               ("visible", NOW - timedelta(days=7) + timedelta(microseconds=1))):
            row = await reminders.create(text=label, due_at=NOW - timedelta(days=10), timezone="UTC",
                                         channel_id="channel-lark", chat="ou_synthetic", created_channel="web",
                                         created_message_id=None)
            await reminders.settle(pool, row["id"], "missed", now=settled, from_status="pending")
        assert [row["text"] for row in await reminders.needing_attention(now=NOW)] == ["visible"]
        assert await reminders.needing_attention(now=NOW + timedelta(microseconds=1)) == []

    run_db(check, monkeypatch)


async def schedule(pool, schedule_id, **columns):
    names = ["id", "project_id", "name", "cron", "timezone", "requirement", *columns]
    values = [schedule_id, "proj", schedule_id, "0 9 * * *", "UTC", "Check it", *columns.values()]
    await pool.execute(f"INSERT INTO schedules ({', '.join(names)}) "
                       f"VALUES ({', '.join(f'${i + 1}' for i in range(len(names)))})", *values)


def test_gates_lists_the_gates_that_wait_for_the_user(monkeypatch):
    async def check(pool):
        await sup.seed(pool)  # task-1's gate-1 is open
        await pool.execute("INSERT INTO task_steps (id, task_id, step_index, instruction, kind) "
                           "VALUES ('gate-2', 'task-1', 1, 'Ask again', 'gate')")
        await pool.execute("INSERT INTO task_user_gates (step_id, task_id, topic, state, summary) "
                           "VALUES ('gate-2', 'task-1', 'Settled already', 'passed', '{}')")
        summary = await attention.collect()
        gates = section(summary, "gates")
        assert (gates["state"], gates["count"], titles(summary, "gates")) == ("ok", 1, ["Pick a database"])
        (item,) = gates["items"]
        assert item["context"] == "Do the thing" and item["href"].startswith("#tasks/task-1?gate=")

    run_db(check, monkeypatch, timeout=20)


def test_schedules_lists_failures_in_a_row_and_active_alerts(monkeypatch):
    async def check(pool):
        await sup.seed(pool)
        await schedule(pool, "nightly", consecutive_failures=2)
        await schedule(pool, "weekly", stale_alert_at=NOW - timedelta(hours=3))
        await schedule(pool, "healthy")
        summary = await attention.collect()
        found = {item["title"]: item for item in section(summary, "schedules")["items"]}
        assert set(found) == {"nightly", "weekly"}
        assert (found["nightly"]["facts"]["failures"], found["nightly"]["facts"]["alerts"]) == (2, [])
        assert found["weekly"]["facts"]["alerts"] == ["stale"] and found["weekly"]["href"] == "#schedules/weekly"

    run_db(check, monkeypatch, timeout=20)


def test_intents_lists_the_pending_ones_while_intents_are_on(monkeypatch):
    async def check(pool):
        await stream.source(pool)
        event = await stream.add(pool, "Synthetic request", sent_at=NOW)
        await leads.intent(pool, [event], title="Fix the login page")
        await leads.intent(pool, [event], title="Already a task", state="tasked")
        assert section(await attention.collect(), "intents") is None  # off: no section
        await setting(pool, "intents.enabled")
        summary = await attention.collect()
        assert titles(summary, "intents") == ["Fix the login page"]
        assert section(summary, "intents")["items"][0]["ref"] == "I-0001"

    run_db(check, monkeypatch, timeout=20)


def test_ledger_lists_its_own_items_that_wait_for_the_user_while_it_is_on(monkeypatch):
    async def check(pool):
        seeded = await sup.seed(pool)
        await sup.add_own(pool, "pending", title="Reply by Friday")
        await sup.add_own(pool, "decision", sup.RELAYED, title="Use PostgreSQL")  # waits for the user's confirmation
        await sup.add_own(pool, "decision", title="Ship on Monday")  # the user's own: confirmed
        await sup.add_own(pool, "work_item", title="Write the report", due_at=NOW - timedelta(days=1))
        await sup.add_own(pool, "work_item", title="Plan next week", due_at=NOW + timedelta(days=1))
        await sup.add_own(pool, "pending", title="Someone else's", owner_kind="manager", owner_ref="manager")
        done = await sup.add_own(pool, "pending", title="Done already")
        await ledger.apply("close", sup.USER, item_id=done["number"], db=pool)
        await sup.add_sourced(pool, seeded, "gate")  # listed under gates
        assert section(await attention.collect(), "ledger") is None  # off: no section
        await setting(pool, "ledger.enabled")
        items = section(await attention.collect(), "ledger")["items"]
        assert [(item["title"], item["facts"]["overdue"]) for item in items] == [
            ("Write the report", True), ("Reply by Friday", False), ("Use PostgreSQL", False)]
        assert all(item["ref"].startswith("L-") for item in items)

    run_db(check, monkeypatch, timeout=20)


def test_deliveries_lists_what_may_not_have_arrived_in_the_window(monkeypatch):
    async def check(pool):
        await sup.seed(pool)
        await stream.source(pool)
        event = await stream.add(pool, "Synthetic request", sent_at=NOW)
        intent = await leads.intent(pool, [event], title="Fix the login page")
        old = await leads.intent(pool, [event], title="Long ago")
        for intent_id, state, at in ((intent, "uncertain", NOW), (intent, "sent", NOW),
                                     (old, "uncertain", NOW - attention.DELIVERY_WINDOW - timedelta(hours=1))):
            await pool.execute("INSERT INTO intent_posts (intent_id, kind, channel_id, chat, text, state, updated_at) "
                               "VALUES ($1, 'push', 'channel-lark', 'ou_me', 'Push', $2, $3)", intent_id, state, at)
        await pool.execute(
            "INSERT INTO manager_events (manager_id, kind, task_id, origin, target_kind, channel_id, chat, dedupe_key, "
            "payload, fallback, state, batch, delivery) VALUES ('manager', 'task_end', 'task-1', 'chat', 'dm', "
            "'channel-lark', 'ou_me', 'end-1', '{}', '[]', 'delivered', gen_random_uuid(), 'uncertain')")
        await schedule(pool, "digest", kind="subscription")
        for run in ("run-1", "run-2"):
            await pool.execute("INSERT INTO schedule_runs (id, schedule_id, scheduled_for, trigger, status) "
                               "VALUES ($1, 'digest', now(), 'scheduled', 'completed')", run)
        await pool.execute("INSERT INTO subscriptions (schedule_id, source_id, chat_kinds, sender_kinds, "
                           "max_age_minutes, max_events, text_max_chars) "
                           "VALUES ('digest', $1, '{group}', '{user}', 60, 10, 200)", stream.SOURCE)
        for run, outcome in (("run-1", "uncertain"), ("run-2", "delivered")):
            await pool.execute("INSERT INTO subscription_runs (run_id, from_seq, to_seq, outcome, target_channel) "
                               "VALUES ($1, 0, 1, $2, 'lark')", run, outcome)
        for run, segment, state in (("run-1", 1, "uncertain"), ("run-1", 2, "uncertain"), ("run-2", 1, "sent")):
            # one push in two messages is one item; a sent one is none
            await pool.execute("INSERT INTO subscription_deliveries (run_id, segment, text, state) "
                               "VALUES ($1, $2, 'Part', $3)", run, segment, state)
        monkeypatch.setattr(attention, "SHOWN", 2)
        limited = section(await attention.collect(), "deliveries")
        assert (limited["count"], len(limited["items"])) == (3, 2)  # counted in full, listed up to SHOWN
        monkeypatch.setattr(attention, "SHOWN", 20)
        items = section(await attention.collect(), "deliveries")["items"]
        assert sorted((item["facts"]["source"], item["title"], item["href"]) for item in items) == [
            ("intent", "Fix the login page", "#intents"), ("subscription", "digest", "#schedules/digest"),
            ("task_end", "Do the thing", "#tasks/task-1")]

    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize("reader", ["deliveries", "earliest"])
def test_catchup_without_debt_does_not_rerun_status_or_its_database_reads(monkeypatch, reader):
    async def check(pool):
        reads = []

        async def status_checks():
            reads.append(await pool.fetchval("SELECT 1"))
            return []

        monkeypatch.setattr(attention, "_status_checks", status_checks)
        monkeypatch.setattr(attention, "_catchup_checks", lambda: [], raising=False)
        # Positive control: the configured login reader really executes its PG read.
        await attention._logins(None)
        assert reads == [1]
        reads.clear()
        if reader == "deliveries":
            assert await attention._deliveries(None) == ([], 0)
        else:
            assert await attention.earliest("deliveries") is None
        assert reads == []

    run_db(check, monkeypatch, timeout=20)


def test_dropped_catchup_windows_stay_in_deliveries_until_recovered(monkeypatch):
    checks = [
        {"id": "lark_catchup:channel-lark:chat-pending:100:pending", "status": "warn",
         "label": "Lark catch-up pending", "detail": "Pending synthetic window", "fix": "Retrying automatically"},
        {"id": "lark_catchup:channel-lark:chat-dropped:200:dropped", "status": "fail",
         "label": "Lark catch-up dropped", "detail": "chat-dropped: 1970-01-01T00:03:20Z–00:04:20Z; retry_exhausted",
         "fix": "Check the chat and forward any missed messages to Looper."},
    ]

    def read_checks():
        return checks

    async def check(pool):
        monkeypatch.setattr(attention, "_catchup_checks", read_checks, raising=False)
        summary = await attention.collect()
        deliveries = section(summary, "deliveries")
        assert deliveries["count"] == 1
        (item,) = deliveries["items"]
        assert item["title"] == checks[1]["detail"]
        assert item["facts"]["source"] == "catchup" and item["facts"]["state"] == "dropped"
        assert item["facts"]["fix"] == checks[1]["fix"] and item["href"] == "#settings/channels"
        assert await attention.earliest("deliveries") == item
        text = attention.summary_text(summary)
        assert "飞书消息补收失败" in text and "chat-dropped" in text
        monkeypatch.setattr(attention, "SHOWN", 0)
        assert section(await attention.collect(), "deliveries")["count"] == 1
        checks.clear()
        assert section(await attention.collect(), "deliveries")["count"] == 0

    run_db(check, monkeypatch, timeout=20)


def test_confirmations_come_from_each_managers_memory_and_are_unknown_without_them(monkeypatch):
    def ops(*waiting):
        return SimpleNamespace(waiting_confirmations=lambda: list(waiting))

    def waiting(confirmation_id, channel, waited):
        return {"confirmation_id": confirmation_id, "tool": "looper_task_control", "impact": f"Cancel {confirmation_id}",
                "channel": channel, "channel_id": None, "chat": f"{channel}-session-1", "waited_seconds": waited,
                "expires_in_seconds": 600 - waited}

    async def check(pool):
        agents = [SimpleNamespace(agent_id="manager", name="Manager"), SimpleNamespace(agent_id="ops", name="Ops")]
        found = {"manager": ops(waiting("c-new", "web", 10)), "ops": ops(waiting("c-old", "lark", 300))}
        fleet = SimpleNamespace(agents=lambda: agents, ops=found.get)
        items = section(await attention.collect(managers=fleet), "confirmations")["items"]
        assert [(item["ref"], item["context"], item["href"]) for item in items] == [
            ("c-old", "Ops", None), ("c-new", "Manager", "#chat?conversation=web-session-1")]
        assert section(await attention.collect(), "confirmations")["state"] == "unknown"

    run_db(check, monkeypatch, timeout=20)


def test_logins_lists_the_failing_login_checks_and_are_unknown_without_a_reader(monkeypatch):
    async def checks():
        return [{"id": "claude_login", "status": "fail", "label": "Claude login", "detail": "not logged in", "fix": "x"},
                {"id": "codex_login", "status": "warn", "label": "Codex login", "detail": "slow", "fix": ""},
                {"id": "git", "status": "fail", "label": "git", "detail": "missing", "fix": ""}]

    async def check(pool):
        assert section(await attention.collect(), "logins")["state"] == "unknown"
        monkeypatch.setattr(attention, "_status_checks", checks)
        assert titles(await attention.collect(), "logins") == ["Claude login"]

    run_db(check, monkeypatch, timeout=20)


def test_a_source_that_cannot_be_read_is_an_error_not_an_empty_section(monkeypatch):
    async def broken(**_):
        raise ConnectionError("schedules unreadable")

    async def check(pool):
        monkeypatch.setattr(db_schedules, "failing", broken)
        summary = await attention.collect()
        failed = section(summary, "schedules")
        assert failed["state"] == "error" and "schedules unreadable" in failed["error"]
        assert section(summary, "gates")["state"] == "ok"
        assert "没能读取" in attention.summary_text(summary)

    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize("value", [None, "false", "true"])
def test_settings_read_on_a_fresh_database_without_operator_identity(monkeypatch, tmp_path, value):
    async def check(pool):
        if value is not None:
            await setting(pool, attention.ENABLED_KEY, value)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("attention.settings_get")
            assert status == 200, answer
            assert answer["result"] == {"enabled": value == "true"}
            status, answer = await call("attention.settings_get", client=True)
            assert status == 403, answer

    run_db(check, monkeypatch, timeout=60)


def test_the_api_is_the_operators_and_answers_conflict_while_off(monkeypatch, tmp_path):
    async def check(pool):
        await sup.seed(pool)
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("attention.summary")
            assert status == 409 and "not enabled" in str(answer), answer
            await setting(pool, attention.ENABLED_KEY)
            status, answer = await call("attention.summary", client=True)
            assert status == 403, answer
            status, answer = await call("attention.summary")
            assert status == 200, answer
            declared = next(op for op in api_v1.operations() if op.operation_id == "attention.summary")
            api_v1.validate_arguments(declared.result, answer["result"])
            assert answer["result"]["total"] == 1 and titles(answer["result"], "gates") == ["Pick a database"]

    run_db(check, monkeypatch, timeout=60)  # a server start reads the code manifest, slow on a loaded machine


def test_a_database_without_the_uncertain_delivery_indexes_builds_them_online(monkeypatch):
    names = [name for name, _ in connection._UNCERTAIN_DELIVERY_INDEXES]

    async def definitions(pool):
        rows = await pool.fetch("SELECT indexname, indexdef FROM pg_indexes WHERE indexname = ANY($1::text[]) "
                                "AND schemaname = current_schema()", names)
        return {row["indexname"]: row["indexdef"] for row in rows}

    async def check(pool):
        fresh = await definitions(pool)
        assert set(fresh) == set(names) and all("WHERE" in definition for definition in fresh.values())
        await pool.execute("; ".join(f"DROP INDEX {name}" for name in names))
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert await definitions(pool) == fresh  # built online, as a new database has them

    run_db(check, monkeypatch, timeout=30)


def test_an_index_a_stopped_build_left_invalid_is_built_again(monkeypatch):
    async def check(pool):
        await sup.seed(pool)
        fresh = await pool.fetchval("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_manager_events_uncertain' "
                                    "AND schemaname = current_schema()")
        await pool.execute("; ".join(f"DROP INDEX {name}" for name, _ in connection._UNCERTAIN_DELIVERY_INDEXES))
        for key in ("end-1", "end-2"):
            await pool.execute(
                "INSERT INTO manager_events (manager_id, kind, task_id, origin, target_kind, channel_id, chat, "
                "dedupe_key, payload, fallback, state, batch, delivery) VALUES ('manager', 'task_end', 'task-1', "
                "'chat', 'dm', 'channel-lark', 'ou_me', $1, '{}', '[]', 'delivered', gen_random_uuid(), 'uncertain')",
                key)
        # A concurrent build that fails leaves its index behind, INVALID, under the name the step builds.
        with pytest.raises(asyncpg.UniqueViolationError):
            await pool.execute("CREATE UNIQUE INDEX CONCURRENTLY idx_manager_events_uncertain "
                               "ON manager_events (channel_id) WHERE delivery = 'uncertain'")
        valid = "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('idx_manager_events_uncertain')"
        assert await pool.fetchval(valid) is False
        await connection.ensure_schema(pool)
        assert await pool.fetchval(valid) is True
        assert await pool.fetchval("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_manager_events_uncertain' "
                                   "AND schemaname = current_schema()") == fresh

    run_db(check, monkeypatch, timeout=60)


def test_a_valid_index_of_another_definition_is_built_again_and_a_matching_one_kept(monkeypatch):
    async def index(pool, name):
        return await pool.fetchrow("SELECT c.oid, pg_get_indexdef(c.oid) AS definition FROM pg_class c "
                                   "WHERE c.oid = to_regclass($1)", name)

    async def check(pool):
        fresh = await index(pool, "idx_manager_events_uncertain")
        kept = await index(pool, "idx_subscription_deliveries_uncertain")
        # An older definition under the step's name, valid; and one index missing, so the migration runs.
        await pool.execute("DROP INDEX idx_manager_events_uncertain; DROP INDEX idx_intent_posts_uncertain; "
                           "CREATE INDEX idx_manager_events_uncertain ON manager_events (task_id)")
        await connection.ensure_schema(pool)
        rebuilt = await index(pool, "idx_manager_events_uncertain")
        assert rebuilt["definition"].split(" USING ", 1)[1] == fresh["definition"].split(" USING ", 1)[1]
        assert await index(pool, "idx_subscription_deliveries_uncertain") == kept  # same oid: not rebuilt
        assert await pool.fetchval("SELECT to_regclass('idx_intent_posts_uncertain') IS NOT NULL")

    run_db(check, monkeypatch, timeout=60)
