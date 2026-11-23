"""Idle delivery claims, restart recovery and schema widening on disposable PostgreSQL."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import idle_checks as ic, proactive_delivery as delivery
from looper.agents.channels.base import PostOutcome, PostReceipt
from looper.agents.tiers import idle_check_turn
from looper.db import connection, idle_checks as store
from tests.db_support import DSN, fresh_connection, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
ITEMS = [{"section": "logins", "key": "login:synthetic", "since": None,
          "title": "Synthetic login", "facts": {}}]


async def running(pool, manager="manager"):
    check_id = await store.insert_running(pool, manager_id=manager, local_date=datetime.now(timezone.utc).date(),
        items=ITEMS, channel_id="channel-lark", chat="ou_synthetic_sending")
    return await store.get(check_id)


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(delivery, "load_policy", lambda: delivery.DeliveryPolicy(
        timezone=timezone.utc, quiet_hours=None))


# Per-case values recorded from deliver_result at 5fb0c4d70, with no later user message.
_POST = [("channel-lark", ("user", "ou_synthetic_sending"),
          "Synthetic attention.\n\n详情：发 /todo，或打开控制台的 Needs you。", "idle-1")]
_BASELINE = [
    ("NO_REPLY", "send", PostOutcome.SENT, ("silent", None), []),
    ("NO_REPLY and text", "send", PostOutcome.SENT, ("failed", "invalid_reply"), []),
    ("Synthetic attention.", "raises", PostOutcome.SENT, ("failed", "delivery_failed"), []),
    ("Synthetic attention.", "digest", PostOutcome.SENT, ("deferred", None), []),
    ("Synthetic attention.", "send", PostOutcome.SENT, ("sent", None), _POST),
    ("Synthetic attention.", "send", PostOutcome.UNKNOWN, ("uncertain", None), _POST),
    ("Synthetic attention.", "send", PostOutcome.REJECTED, ("failed", "send_rejected"), _POST),
]


@pytest.mark.parametrize("reply,decision,outcome,expected,expected_posts", _BASELINE)
def test_unchanged_delivery_outcomes_match_recorded_items(monkeypatch, reply, decision, outcome, expected,
                                                        expected_posts):
    async def run(pool):
        check = await running(pool)
        assert check["id"] == 1
        decide = AsyncMock(return_value=SimpleNamespace(outcome=delivery.Outcome(decision if decision != "raises"
                                                                                 else "send")))
        if decision == "raises":
            decide.side_effect = RuntimeError("synthetic decision failure")
        monkeypatch.setattr(delivery, "decide", decide)
        posts = []

        async def post(channel, target, text, *, idempotency_key):
            posts.append((channel, (target.kind.value, target.id), text, idempotency_key))
            return PostReceipt(outcome is PostOutcome.SENT, outcome=outcome)

        assert await ic.deliver_result(check, reply, post=post) == expected[0]
        row = await store.get(check["id"])
        assert (row["state"], row["reason"]) == expected
        assert posts == expected_posts

    run_db(run, monkeypatch)


def test_cancelled_post_recovers_as_unknown_and_replayed_wake_never_posts(monkeypatch, caplog):
    async def run(pool):
        check = await running(pool)
        post = AsyncMock(side_effect=asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError):
            await ic.deliver_result(check, "Synthetic attention.", post=post)
        row = await store.get(check["id"])
        assert (row["state"], row["reason"]) == ("sending", None)
        assert row["finished_at"] is not None
        before = await pool.fetchrow("SELECT decision, reason, counted, decided_at FROM proactive_deliveries")
        assert before["decision"] == "send"
        service = ic.IdleCheckService(None, idle_for=lambda: 0, manager_running=lambda: True)
        with caplog.at_level("INFO"):
            await service.recover()
        assert (await store.get(check["id"]))["state"] == "uncertain"
        assert await store.mentioned_items(pool, "manager", check["created_at"] - timedelta(seconds=1)) == ITEMS
        receipt = await pool.fetchrow("SELECT * FROM proactive_deliveries")
        assert (receipt["dedupe_key"], receipt["source"], receipt["source_ref"], receipt["manager_id"],
                receipt["channel_id"], receipt["chat"], receipt["chat_kind"], receipt["outcome"]) == (
            "idle-1", "idle_check", "1", "manager", "channel-lark", "ou_synthetic_sending", "direct", "unknown")
        assert tuple(receipt[key] for key in before.keys()) == tuple(before)
        assert any("1" in record.message and "sending" in record.message for record in caplog.records)
        monkeypatch.setattr(idle_check_turn, "_post", post)
        agent = SimpleNamespace(agent_id="manager", _session_now=lambda: SimpleNamespace())
        await idle_check_turn.handle(agent, ic.due_envelope(check["id"]))
        await service.recover()
        post.assert_awaited_once()

    run_db(run, monkeypatch)


def test_a_claim_losing_to_preemption_does_not_post(monkeypatch):
    async def run(pool):
        check = await running(pool)
        claim = store.claim_sending

        async def raced(check_id):
            assert await store.settle(check_id, "preempted")
            return await claim(check_id)

        monkeypatch.setattr(store, "claim_sending", raced)
        post = AsyncMock()
        assert await ic.deliver_result(check, "Synthetic attention.", post=post) == "preempted"
        assert not await claim(check["id"])
        post.assert_not_awaited()

    run_db(run, monkeypatch)


@pytest.mark.parametrize("source,state,reason,allowed", [
    ("running", "sent", None, False), ("running", "uncertain", None, False),
    ("running", "failed", "send_rejected", False), ("running", "failed", "restart", True),
    ("sending", "sent", None, True), ("sending", "uncertain", None, True),
    ("sending", "failed", "send_rejected", True), ("sending", "failed", "restart", False),
    ("sending", "silent", None, False), ("sending", "deferred", None, False),
    ("sending", "preempted", None, False),
])
def test_store_allows_only_explicit_settlement_transitions(monkeypatch, source, state, reason, allowed):
    async def run(pool):
        check = await running(pool)
        if source == "sending":
            assert await store.claim_sending(check["id"])
            assert not await store.claim_sending(check["id"])
        assert await store.settle(check["id"], state, reason=reason) is allowed
        assert (await store.get(check["id"]))["state"] == (state if allowed else source)

    run_db(run, monkeypatch)


def test_sending_is_neither_purged_nor_counted_in_failed_run(monkeypatch):
    async def run(pool):
        sending, failed = await running(pool), await running(pool)
        assert await store.claim_sending(sending["id"])
        assert await store.settle(failed["id"], "failed", reason="turn_failed")
        await pool.execute("UPDATE idle_checks SET created_at = now() - interval '100 days'")
        assert await store.last_states("manager", 3) == ["failed"]
        assert await store.purge(datetime.now(timezone.utc) - timedelta(days=90)) == 1
        assert (await store.get(sending["id"]))["state"] == "sending"

    run_db(run, monkeypatch)


def test_recovery_respects_manager_and_manager_off_only_fails_running(monkeypatch):
    async def run(pool):
        await pool.execute("INSERT INTO managers (id, name, system_prompt) VALUES "
                           "('manager-other', 'Synthetic other', 'Synthetic manager')")
        own, other, waiting = await running(pool), await running(pool, "manager-other"), await running(pool)
        for row in (own, other):
            assert await store.claim_sending(row["id"])
        assert await store.settle_running("manager_off", manager_id="manager") == 1
        recovered = await store.recover_sending(manager_id="manager")
        assert recovered == [{"id": own["id"], "manager_id": "manager", "channel_id": "channel-lark",
                              "chat": "ou_synthetic_sending"}]
        assert (await store.get(other["id"]))["state"] == "sending"
        assert (await store.get(waiting["id"]))["reason"] == "manager_off"
        assert len(await store.recover_sending()) == 1

    run_db(run, monkeypatch)


@pytest.mark.parametrize("failure", ["recovery", "receipt"])
def test_failed_recovery_write_blocks_startup(monkeypatch, failure):
    async def run(pool):
        check = await running(pool)
        assert await store.claim_sending(check["id"])
        failing = AsyncMock(side_effect=RuntimeError("synthetic recovery database unavailable"))
        monkeypatch.setattr(store if failure == "recovery" else ic.db_deliveries,
                            "recover_sending" if failure == "recovery" else "insert", failing)
        with pytest.raises(RuntimeError, match="synthetic recovery database unavailable"):
            await ic.IdleCheckService(None, idle_for=lambda: 0, manager_running=lambda: True).recover()

    run_db(run, monkeypatch)


@pytest.mark.parametrize("validated", [False, True])
def test_migration_repairs_rebuilt_check_and_resumes_validation(validated):
    async def run():
        from looper.db.migrations import idle_check_sending as migration

        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            await conn.execute("ALTER TABLE idle_checks DROP CONSTRAINT idle_checks_state_check")
            await conn.execute("ALTER TABLE idle_checks ADD CONSTRAINT synthetic_widening CHECK (state IN "
                "('running','silent','sent','uncertain','deferred','failed','preempted','other_value'))")
            before = await conn.fetchrow("INSERT INTO idle_checks (manager_id, local_date, items, state, "
                "channel_id, chat, finished_at) VALUES ('manager', CURRENT_DATE, '[{}]', 'other_value', "
                "'channel-lark', 'ou_synthetic_sending', now()) RETURNING id, xmin::text AS version")
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ('idle_check_sending') "
                               "ON CONFLICT DO NOTHING")
            assert await migration.pending(conn)
            await migration.apply(conn, sql="", state="current")
            assert not await migration.pending(conn)
            assert not await conn.fetchval("SELECT convalidated FROM pg_constraint "
                "WHERE conrelid='idle_checks'::regclass AND conname='idle_checks_state_check'")
            if validated:
                await conn.execute("ALTER TABLE idle_checks VALIDATE CONSTRAINT idle_checks_state_check")
            else:
                # Exercise apply's unvalidated branch with an online marker that must be cleared.
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT DO NOTHING",
                                   migration.online[0].marker)
                await migration.apply(conn, sql="", state="current")
                assert not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name=$1)",
                                              migration.online[0].marker)
            await connection._finish_online(conn)
            definition = await conn.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid='idle_checks'::regclass AND conname='idle_checks_state_check'")
            assert "'sending'" in definition and "'other_value'" in definition
            assert await conn.fetchval("SELECT xmin::text FROM idle_checks WHERE id=$1", before["id"]) == before["version"]
            assert await conn.fetchval("SELECT convalidated FROM pg_constraint "
                "WHERE conrelid='idle_checks'::regclass AND conname='idle_checks_state_check'")
            assert await conn.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid='idle_checks'::regclass AND conname='idle_checks_finished'") == (
                    "CHECK (((state = 'running'::text) = (finished_at IS NULL)))")

    asyncio.run(run())


def test_fresh_schema_admits_sending_and_records_only_baseline():
    async def run():
        from looper.db.bridge_upgrade import BASELINE
        from looper.db.migrations import idle_check_sending as migration

        async with fresh_connection() as conn:
            await connection.ensure_schema(conn)
            assert {row["name"] for row in await conn.fetch("SELECT name FROM schema_migrations")} == {BASELINE}
            assert not await migration.pending(conn)
            assert not await connection.schema_change_pending(conn)
            check_id = await store.insert_running(conn, manager_id="manager", local_date=datetime.now(timezone.utc).date(),
                items=ITEMS, channel_id="channel-lark", chat="ou_synthetic_sending")
            assert await store.claim_sending(check_id, pool=conn)
            row = await store.get(check_id, pool=conn)
            assert row["state"] == "sending" and row["finished_at"] is not None

    asyncio.run(run())


def test_disabled_empty_startup_adds_only_the_sending_recovery_query(monkeypatch):
    async def run(pool):
        fetch = type(pool).fetch
        queries = []

        async def recorded(self, query, *args, **kwargs):
            queries.append(query)
            return await fetch(self, query, *args, **kwargs)

        monkeypatch.setattr(type(pool), "fetch", recorded)
        await ic.IdleCheckService(None, idle_for=lambda: 0, manager_running=lambda: True).recover()
        assert queries == [
            "UPDATE idle_checks SET state = 'uncertain', finished_at = now() "
            "WHERE state = 'sending' AND ($1::text IS NULL OR manager_id = $1) "
            "RETURNING id, manager_id, channel_id, chat",
            "UPDATE idle_checks SET state = 'failed', reason = $1, finished_at = now() "
            "WHERE state = 'running' AND ($2::text IS NULL OR manager_id = $2) RETURNING id",
        ]
        assert await pool.fetchval("SELECT count(*) FROM idle_checks") == 0
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 0

    run_db(run, monkeypatch)
