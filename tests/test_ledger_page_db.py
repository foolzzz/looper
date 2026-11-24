"""Ledger section and cursor boundaries against a disposable database."""

import base64
import json
from datetime import datetime, timezone

import pytest

from looper.db import ledger
from tests import ledger_support as sup
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_page_sections_and_attention_count_are_authoritative(monkeypatch):
    async def check(pool):
        seed = await sup.seed(pool)
        gate = await sup.add_sourced(pool, seed, "gate")
        task = await sup.add_sourced(pool, seed, "task")
        pending = await sup.add_own(pool, due_at=datetime(2020, 1, 1, tzinfo=timezone.utc), owner_kind="chat",
                                    owner_ref="proposal")
        relayed = await sup.add_own(pool, "decision", sup.RELAYED)
        confirmed = await sup.add_own(pool, "decision")
        cancelled = await sup.add_own(pool, "decision", sup.RELAYED)
        await ledger.apply("cancel", sup.USER, item_id=cancelled["number"], db=pool)
        old = await sup.add_own(pool, "decision")
        await pool.execute("UPDATE ledger_items SET created_at = now() - interval '31 days' WHERE id = $1", old["number"])
        work = await sup.add_own(pool, "work_item")
        items, more, cursor = await ledger.page_items(db=pool)
        assert [i["id"] for i in items] == [pending["id"], relayed["id"], gate["id"]]
        assert more == 0 and cursor is None
        assert await ledger.attention_count(db=pool) == 3
        items, _, _ = await ledger.page_items(section="work", db=pool)
        assert [i["id"] for i in items] == [work["id"], task["id"]]
        items, _, _ = await ledger.page_items(section="recent_decisions", db=pool)
        assert [(i["id"], i["status"]) for i in items] == [(cancelled["id"], "cancelled"), (confirmed["id"], "open")]
        items, _, _ = await ledger.page_items(section="all", kind="decision", status="all", db=pool)
        assert len(items) == 4 and await ledger.attention_count(db=pool) == 3
        await pool.execute("UPDATE task_user_gates SET state = 'passed', summary = '{}'::jsonb WHERE step_id = 'gate-1'")
        assert await ledger.attention_count(db=pool) == 2
        items, _, _ = await ledger.page_items(db=pool)
        assert gate["id"] not in [i["id"] for i in items]
        assert await pool.fetchval("SELECT status IS NULL FROM ledger_items WHERE id = $1", gate["number"])
    run_db(check, monkeypatch, timeout=30)


def test_page_cursor_keeps_equal_due_and_null_due_items(monkeypatch):
    async def check(pool):
        due = datetime(2030, 1, 1, tzinfo=timezone.utc)
        seeded = [await sup.add_own(pool, title=f"Proposal {number}", due_at=due if number < 17 else None)
                  for number in range(23)]
        items, more, cursor = await ledger.page_items(limit=10, db=pool)
        assert len(items) == 10 and more == 13 and cursor
        first = items
        new = await sup.add_own(pool, title="New proposal", due_at=due)
        later_due = await sup.add_own(pool, title="New future proposal", due_at=datetime(2031, 1, 1, tzinfo=timezone.utc))
        while cursor:
            batch, remaining, next_cursor = await ledger.page_items(limit=10, cursor=cursor, db=pool)
            assert remaining == max(0, 23 - len(items) - len(batch))
            assert bool(next_cursor) == bool(remaining)
            items += batch
            cursor = next_cursor
        expected = list(reversed(seeded[:17])) + list(reversed(seeded[17:]))
        assert [i["id"] for i in items] == [i["id"] for i in expected]
        assert later_due["id"] not in [i["id"] for i in items]
        refreshed, _, cursor = await ledger.page_items(limit=10, db=pool)
        assert refreshed[0]["id"] == new["id"] and len(first) >= 10
        recorded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        for kwargs in ({"section": "all"}, {"kind": "decision"}, {"owner": "user"}, {"status": "all"}):
            with pytest.raises(ledger.LedgerRefused) as failure:
                await ledger.page_items(cursor=cursor, db=pool, **kwargs)
            assert failure.value.code == "invalid"
        bad_values = ["?", "e30", "x" * 2049, base64.urlsafe_b64encode(json.dumps({"v": 2}).encode()).decode()]
        for key, value in [("version", True), ("version", 2), ("number", 0), ("number", True),
                           ("number", 2 ** 63), ("ceiling", -1), ("due_at", "2030-01-01"),
                           ("due_at", 1), ("extra", "unexpected")]:
            bad_values.append(base64.urlsafe_b64encode(json.dumps({**recorded, key: value}).encode()).decode())
        for bad in bad_values:
            with pytest.raises(ledger.LedgerRefused) as failure:
                await ledger.page_items(cursor=bad, db=pool)
            assert failure.value.code == "invalid"
        all_items, _, all_cursor = await ledger.page_items(section="all", limit=10, db=pool)
        assert all_cursor
        later = await sup.add_own(pool)
        rest, _, _ = await ledger.page_items(section="all", limit=50, cursor=all_cursor, db=pool)
        assert later["id"] not in [i["id"] for i in rest]
        assert not ({i["id"] for i in rest} & {i["id"] for i in all_items})
    run_db(check, monkeypatch, timeout=35)
