"""API session persistence and deletion against disposable PostgreSQL."""

import asyncio
import json

import pytest

from looper.db import agent_sessions
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

PROTOCOL = "openai-responses"


def message(text):
    return {"version": 1, "kind": "message", "role": "user", "text": text}


def test_append_load_preserves_neutral_order_and_originating_model():
    from looper.db import api_sessions

    async def run(pool):
        await api_sessions.append("session", 1, PROTOCOL, "first-model", [message("one"), message("two")], pool=pool)
        await api_sessions.append("session", 2, PROTOCOL, "second-model", [message("three")], pool=pool)
        rows = await api_sessions.load("session", pool=pool)
        assert [json.loads(row["item"]) for row in rows] == [message("one"), message("two"), message("three")]
        assert [(row["seq"], row["turn"], row["model"], row["protocol"]) for row in rows] == [
            (1, 1, "first-model", PROTOCOL), (2, 1, "first-model", PROTOCOL), (3, 2, "second-model", PROTOCOL)]

    run_db(run, timeout=15)


def test_failed_first_append_rolls_back_metadata_and_items():
    from looper.db import api_sessions

    async def run(pool):
        with pytest.raises(TypeError):
            await api_sessions.append("session", 1, PROTOCOL, "model", [message("one"), object()], pool=pool)
        assert await pool.fetchval("SELECT count(*) FROM api_sessions") == 0
        assert await pool.fetchval("SELECT count(*) FROM api_session_items") == 0

    run_db(run, timeout=15)


def test_protocol_mismatch_preserves_committed_history():
    from looper.db import api_sessions

    async def run(pool):
        await api_sessions.append("session", 1, PROTOCOL, "model", [message("one")], pool=pool)
        before = await api_sessions.load("session", pool=pool)
        with pytest.raises(api_sessions.TranscriptMismatch):
            await api_sessions.append("session", 2, "openai-chat", "model", [message("two")], pool=pool)
        assert await api_sessions.load("session", pool=pool) == before

    run_db(run, timeout=15)


def test_concurrent_appends_allocate_contiguous_sequences():
    from looper.db import api_sessions

    async def run(pool):
        await asyncio.gather(*(
            api_sessions.append("session", turn, PROTOCOL, "model", [message(str(turn)), message(str(turn))], pool=pool)
            for turn in (1, 2, 3)))
        rows = await api_sessions.load("session", pool=pool)
        assert [row["seq"] for row in rows] == list(range(1, 7))
        assert all(rows[i]["turn"] == rows[i + 1]["turn"] for i in (0, 2, 4))

    run_db(run, timeout=15)


def test_load_missing_session_does_not_create_metadata():
    from looper.db import api_sessions

    async def run(pool):
        assert await api_sessions.load("missing", pool=pool) == []
        assert await api_sessions.last_turn("missing", pool=pool) is None
        assert await pool.fetchval("SELECT count(*) FROM api_sessions") == 0

    run_db(run, timeout=15)


def test_last_turn_reads_highest_stored_turn():
    from looper.db import api_sessions

    async def run(pool):
        await api_sessions.append("session", 1, PROTOCOL, "model", [message("one")], pool=pool)
        await api_sessions.append("session", 2, PROTOCOL, "model", [message("two")], pool=pool)
        assert await api_sessions.last_turn("session", pool=pool) == 2

    run_db(run, timeout=15)


def test_delete_removes_metadata_and_cascades_items():
    from looper.db import api_sessions

    async def run(pool):
        await api_sessions.append("session", 1, PROTOCOL, "model", [message("one"), message("two")], pool=pool)
        assert await api_sessions.delete("session", pool=pool) == 2
        assert await pool.fetchval("SELECT count(*) FROM api_sessions") == 0
        assert await pool.fetchval("SELECT count(*) FROM api_session_items") == 0
        assert await api_sessions.delete("session", pool=pool) == 0

    run_db(run, timeout=15)


@pytest.mark.parametrize("new_session", [None, "replacement"])
def test_replacing_api_session_deletes_its_persisted_history(new_session):
    from looper.db import api_sessions

    async def run(pool):
        await api_sessions.append("old", 1, PROTOCOL, "model", [message("one")], pool=pool)
        await agent_sessions.save_session("manager", "old", {}, runtime_type="api", pool=pool)
        await agent_sessions.save_session("manager", new_session, {}, runtime_type="api", pool=pool)
        assert await pool.fetchval("SELECT count(*) FROM api_sessions") == 0
        assert await pool.fetchval("SELECT count(*) FROM api_session_items") == 0
        assert (await agent_sessions.get_session("manager", pool=pool))["session_id"] == new_session

    run_db(run, timeout=15)


def test_cli_session_replacement_does_not_delete_api_history():
    from looper.db import api_sessions

    async def run(pool):
        await api_sessions.append("old", 1, PROTOCOL, "model", [message("one")], pool=pool)
        await agent_sessions.save_session("manager", "old", {}, runtime_type="claude-code", pool=pool)
        await agent_sessions.save_session("manager", "new", {}, runtime_type="claude-code", pool=pool)
        assert len(await api_sessions.load("old", pool=pool)) == 1

    run_db(run, timeout=15)


def test_same_api_session_keeps_history():
    from looper.db import api_sessions

    async def run(pool):
        await api_sessions.append("session", 1, PROTOCOL, "model", [message("one")], pool=pool)
        await agent_sessions.save_session("manager", "session", {}, runtime_type="api", pool=pool)
        await agent_sessions.save_session("manager", "session", {"cost_usd": 1}, runtime_type="api", pool=pool)
        assert len(await api_sessions.load("session", pool=pool)) == 1

    run_db(run, timeout=15)


def test_failed_session_replacement_preserves_old_history_and_pointer():
    from looper.db import api_sessions

    async def run(pool):
        await api_sessions.append("old", 1, PROTOCOL, "model", [message("one")], pool=pool)
        await agent_sessions.save_session("manager", "old", {}, runtime_type="api", pool=pool)
        with pytest.raises(TypeError):
            await agent_sessions.save_session("manager", "new", {"bad": object()}, runtime_type="api", pool=pool)
        assert len(await api_sessions.load("old", pool=pool)) == 1
        assert (await agent_sessions.get_session("manager", pool=pool))["session_id"] == "old"

    run_db(run, timeout=15)


@pytest.mark.parametrize("runtime_type", ["api", "claude-code"])
def test_deleting_manager_removes_only_its_api_session_history(monkeypatch, runtime_type):
    from looper.db import api_sessions, managers

    async def run(pool):
        async with pool.acquire() as conn:
            await managers.insert_manager({"id": "manager-review", "name": "Review",
                                           "system_prompt": "Review code."}, conn=conn)
        for session_id in ("review-session", "other-session"):
            await api_sessions.append(session_id, 1, PROTOCOL, "model", [message("retained fact")], pool=pool)
        await agent_sessions.save_session("manager-review", "review-session", {}, runtime_type=runtime_type)
        await agent_sessions.save_session("manager", "other-session", {}, runtime_type="api")
        assert await pool.fetchval("SELECT count(*) FROM api_sessions") == 2
        assert await pool.fetchval("SELECT count(*) FROM api_session_items") == 2

        assert await managers.delete_manager("manager-review") == "deleted"
        assert await agent_sessions.get_session("manager-review") is None
        assert await managers.get_manager("manager-review") is None
        remaining = 0 if runtime_type == "api" else 1
        assert await pool.fetchval(
            "SELECT count(*) FROM api_sessions WHERE session_id = 'review-session'") == remaining
        assert len(await api_sessions.load("review-session", pool=pool)) == remaining
        assert len(await api_sessions.load("other-session", pool=pool)) == 1
        assert (await agent_sessions.get_session("manager"))["session_id"] == "other-session"

    run_db(run, monkeypatch, timeout=15)


@pytest.mark.parametrize("table,event", [("api_sessions", "DELETE"), ("managers", "UPDATE")])
def test_failed_manager_deletion_rolls_back_api_history_and_pointer(monkeypatch, table, event):
    import asyncpg
    from looper.db import api_sessions, managers

    async def run(pool):
        async with pool.acquire() as conn:
            await managers.insert_manager({"id": "manager-review", "name": "Review",
                                           "system_prompt": "Review code."}, conn=conn)
        await managers.put_template("manager-review", "review", {"name": "Review"})
        await api_sessions.append("review-session", 1, PROTOCOL, "model", [message("retained fact")], pool=pool)
        await agent_sessions.save_session("manager-review", "review-session", {}, runtime_type="api")
        before = await api_sessions.load("review-session", pool=pool)
        # Fail either history cleanup or the final Manager write in the real transaction.
        await pool.execute("""
            CREATE FUNCTION reject_deletion() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected deletion failure' USING ERRCODE = '23514'; END $$
        """)
        await pool.execute(f"CREATE TRIGGER reject_deletion BEFORE {event} ON {table} "
                           "FOR EACH ROW EXECUTE FUNCTION reject_deletion()")
        with pytest.raises(asyncpg.CheckViolationError, match="injected deletion failure"):
            await managers.delete_manager("manager-review")
        assert (await managers.get_manager("manager-review"))["enabled"] is True
        assert len(await managers.list_templates("manager-review")) == 1
        assert (await agent_sessions.get_session("manager-review"))["session_id"] == "review-session"
        assert await pool.fetchval("SELECT count(*) FROM api_sessions") == 1
        assert await api_sessions.load("review-session", pool=pool) == before

    run_db(run, monkeypatch, timeout=15)
