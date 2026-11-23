"""Shared card authorization, transactional resolution and the needs-you readback."""

import asyncio
from datetime import timedelta

import pytest

from looper.agents import attention, card_actions
from looper.db import card_actions as store, connection, system_settings
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def create(pool, kind="confirmation", token="test-card", subject="c-1"):
    card = card_actions.Card("Confirm the operation", (card_actions.Button("确认", f"{token}:confirm"),))
    await store.create(token=token, kind=kind, subject=subject, digest="snapshot",
                       payload={"tool": "looper_projects", "impact": "Delete sample",
                                "turn": {"channel": "web", "chat": "session-1"}, "card": card.to_dict()},
                       manager_id="manager", channel_id="channel-telegram", chat="7", allowed=["7"],
                       ttl=timedelta(minutes=10), pool=pool)


def test_upgrade_widens_card_kinds_without_losing_old_cards(monkeypatch):
    async def check(pool):
        await create(pool)
        await pool.execute("ALTER TABLE card_actions DROP CONSTRAINT card_actions_kind_check")
        await pool.execute("ALTER TABLE card_actions ADD CONSTRAINT card_actions_kind_check "
                           "CHECK (kind IN ('confirmation', 'gate'))")
        await pool.execute("DELETE FROM schema_migrations WHERE name='card_actions_intents_ledger'")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        await connection.ensure_schema(pool)
        for kind in ("intent", "ledger"):
            await create(pool, kind=kind, token=kind)
        assert await pool.fetchval("SELECT count(*) FROM card_actions") == 3

    run_db(check, monkeypatch, timeout=30)


def test_business_transaction_rolls_back_card_claim_and_resolution(monkeypatch):
    async def check(pool):
        await create(pool, kind="ledger")
        with pytest.raises(RuntimeError):
            async with pool.acquire() as conn, conn.transaction():
                assert await store.claim("test-card", by="7", via="button", choice="confirm", outcome="ok", pool=conn)
                raise RuntimeError("Business operation failed")
        assert (await store.get("test-card", pool=pool))["handled_at"] is None
        async with pool.acquire() as conn, conn.transaction():
            await store.resolve_subject("ledger", "c-1", by="cli", via="typed", choice="confirm", outcome="ok", pool=conn)
        assert not await store.claim("test-card", by="7", via="button", choice="confirm", outcome="ok", pool=pool)
        assert (await store.get("test-card", pool=pool))["handled_via"] == "typed"

    run_db(check, monkeypatch, timeout=20)


def test_kind_authorization_ends_before_the_common_deadline(monkeypatch):
    acted = []

    async def authorize(row, event, choice):
        await asyncio.sleep(0.2)

    async def handler(row, event, choice):
        acted.append(choice)
        return card_actions.Answer("ok")

    async def check(pool):
        await system_settings.put_settings({card_actions.ENABLED_KEY: "true"}, pool=pool)
        await create(pool)
        monkeypatch.setattr(card_actions, "_handlers", {})
        monkeypatch.setattr(card_actions, "_authorizers", {})
        card_actions.install("confirmation", handler, authorize=authorize)
        answer = await card_actions.press(card_actions.Press("channel-telegram", "7", "test-card:confirm", chat="7"),
                                          check_until=asyncio.get_running_loop().time() + 0.05)
        assert answer.toast == card_actions.UNCHECKED
        await asyncio.sleep(0.25)
        assert acted == [] and (await store.get("test-card", pool=pool))["handled_at"] is None

    run_db(check, monkeypatch, timeout=20)


def test_needs_you_reads_persisted_confirmations_without_a_running_manager(monkeypatch):
    async def check(pool):
        await system_settings.put_settings({card_actions.ENABLED_KEY: "true"}, pool=pool)
        await create(pool)
        await pool.execute("UPDATE card_actions SET channel_id=NULL, chat='session-1'")
        summary = await attention.collect()
        section = next(s for s in summary["sections"] if s["kind"] == "confirmations")
        assert section["state"] == "ok" and section["count"] == 1
        assert section["items"][0]["ref"] == "c-1"
        assert section["items"][0]["href"] == "#chat?conversation=session-1"
        earliest = await attention.earliest("confirmations")
        assert earliest["ref"] == "c-1" and earliest["href"] == section["items"][0]["href"]
        await store.claim("test-card", by="7", via="button", choice="confirm", outcome="ok", pool=pool)
        section = next(s for s in (await attention.collect())["sections"] if s["kind"] == "confirmations")
        assert section["state"] == "ok" and section["count"] == 0
        assert await attention.earliest("confirmations") is None

    run_db(check, monkeypatch, timeout=20)


def test_needs_you_does_not_duplicate_persisted_or_resurrect_resolved_confirmations(monkeypatch):
    from types import SimpleNamespace
    from looper.agents.ops import Turn
    from looper.agents.ops.service import ManagerOps, Platform
    from looper.db import projects

    async def check(pool):
        await system_settings.put_settings({card_actions.ENABLED_KEY: "true"}, pool=pool)
        await projects.create_project(name="sample", pool=pool)
        ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:1")
        turn = Turn(channel="web", chat="session-1", message_id="request")
        ops.begin_turn(turn)
        asked = await ops.call("looper_projects", {"action": "delete", "project": "sample"})
        ops.end_turn(turn)
        assert asked["status"] == "needs_confirmation"
        fleet = SimpleNamespace(agents=lambda: [SimpleNamespace(agent_id="manager", name="Manager")],
                                ops=lambda _: ops)
        assert len(await card_actions.pending_confirmations(fleet)) == 1
        token = await pool.fetchval("SELECT token FROM card_actions")
        assert await store.claim(token, by="operator", via="button", choice="confirm", outcome="ok", pool=pool)
        assert ops.waiting_confirmations()  # memory deliberately still has the old question
        assert await card_actions.pending_confirmations(fleet) == []

    run_db(check, monkeypatch, timeout=20)
