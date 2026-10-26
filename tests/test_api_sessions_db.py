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
