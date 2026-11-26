"""Restore annotations are per-agent and per-session persistent state."""

import json

import pytest

from looper.agents import chat_settings, compaction_restore as restore
from looper.db import llm_records, system_settings
from tests.db_support import run_db


def test_latest_restore_follows_annotations_across_busy_chats(monkeypatch):
    async def check(pool):
        async def add(state=None, *, agent="planner-task", session="session", **facts):
            metadata = {"compaction_restore": {"state": state, **facts}} if state else {}
            return await pool.fetchval("INSERT INTO llm_records (agent_id, session_id, input_text, metadata) "
                                       "VALUES ($1, $2, 'turn', $3::jsonb) RETURNING id",
                                       agent, session, json.dumps(metadata))

        async def latest():
            return restore.pending(await llm_records.latest_compaction_restore("planner-task", "session", pool=pool))

        assert await latest() is None
        due = await add("due", trigger="manual", pre_tokens=100)
        assert await latest() == restore.Due(due, "manual", 100)
        await add("offered", due_record=due, trigger="manual", pre_tokens=100)
        assert await latest() == restore.Due(due, "manual", 100)
        await add("sent", due_record=due)
        assert await latest() is None
        due = await add("due", trigger="auto")
        await add("due", session="other")
        await add("due", agent="other")
        assert await latest() == restore.Due(due, "auto")
        await pool.execute("UPDATE llm_records SET created_at='2020-01-01'")
        for _ in range(501):
            await add(session="other")
        assert await latest() == restore.Due(due, "auto")
        await add("sent", due_record=due)
        assert await latest() is None

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("value,expected", [(None, False), ("false", False), ("yes", False), ("TRUE", False), ("true", True)])
def test_setting_only_accepts_exact_stored_true(monkeypatch, value, expected):
    async def check(pool):
        if value is not None:
            await system_settings.put_settings({restore.KEY: value}, pool=pool)
        assert await restore.enabled(pool) is expected
        await chat_settings.load()
        assert restore.cached_enabled() is expected
    run_db(check, monkeypatch, timeout=30)
