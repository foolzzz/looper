"""Operator-only lesson foundation contracts, with positive project data."""
import json

import pytest

from tests import memory_support as sup
from tests.memory_lessons_support import lesson as _lesson
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")




def test_operator_lists_only_project_lessons_with_pagination(monkeypatch, tmp_path):
    async def check(pool):
        first = await _lesson(pool, "project-a", "First")
        second = await _lesson(pool, "project-a", "Second")
        await _lesson(pool, "project-b", "Other")
        await sup.candidate(pool)
        await pool.execute("INSERT INTO projects(id,name) VALUES ('project-empty','Empty')")
        await pool.execute("INSERT INTO memory_lesson_stats(candidate_id,injected,cited) VALUES ($1,3,1)",
                           int(second[2:]))
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("memory.lessons_list", {"project": "project-a", "limit": 1})
            assert status == 200, answer
            result = answer["result"]
            assert result["more"] and result["lessons"][0]["candidate"]["id"] == second
            assert result["lessons"][0]["stats"]["injected"] == 3
            status, answer = await call("memory.lessons_list", {"project": "project-a", "limit": 1, "before": second})
            assert status == 200 and not answer["result"]["more"], answer
            assert answer["result"]["lessons"][0]["candidate"]["id"] == first
            assert answer["result"]["lessons"][0]["stats"] == {
                "injected": 0, "cited": 0, "wrong_tasks": 0, "reinforced": 0,
                "last_injected_at": None, "last_cited_at": None}
            status, answer = await call("memory.lessons_list", {"project": "project-empty"})
            assert status == 200 and answer["result"] == {"lessons": [], "more": False}
            status, answer = await call("memory.lessons_list", {"state": "in_effect"})
            assert status == 200 and answer["result"]["lessons"] == [], answer
            for body, expected in (({"project": "missing"},404), ({"state": "pending"},400),
                                   ({"before": "42"},400), ({"limit": 0},400), ({"limit": 201},400)):
                status, answer = await call("memory.lessons_list", body)
                assert status == expected, answer
    run_db(check, monkeypatch, timeout=60)


def test_settings_are_confirmed_and_clients_are_refused(monkeypatch, tmp_path):
    async def check(pool):
        async with v1_api(tmp_path, pool) as call:
            status, answer = await call("memory_lessons.settings_get")
            assert status == 200 and not answer["result"]["configured"] and not answer["result"]["enabled"]
            assert answer["result"]["settings"]["max_lessons"] == 12
            status, answer = await call("memory_lessons.settings_put", {"enabled": True})
            assert status == 200 and answer["result"]["enabled"], answer
            saved = await pool.fetchval("SELECT value FROM system_settings WHERE key='memory_lessons'")
            for body in ({"max_lessons": True}, {"unknown": True}, {"weekly": {"time": "9:30"}}):
                status, answer = await call("memory_lessons.settings_put", body)
                assert status == 400, answer
                assert await pool.fetchval("SELECT value FROM system_settings WHERE key='memory_lessons'") == saved
            status, asked = await call("memory_lessons.settings_put", {"enabled": True,"auto_activate": True})
            assert status == 202, asked
            assert await pool.fetchval("SELECT value FROM system_settings WHERE key='memory_lessons'") == saved
            status, answer = await call("confirm", {"confirmation_id": asked["confirmation_id"]})
            assert status == 200 and answer["result"]["settings"]["auto_activate"], answer
            for operation in ("memory.lessons_list", "memory_lessons.settings_get", "memory_lessons.settings_put"):
                status, answer = await call(operation, client=True)
                assert status == 403, answer
            status, answer = await call("memory_lessons.settings_put", {"actor": "user"})
            assert status == 400
            await pool.execute("UPDATE system_settings SET value='invalid' WHERE key='memory_lessons'")
            status, answer = await call("memory_lessons.settings_get")
            assert status == 200 and not answer["result"]["enabled"] and answer["result"]["problem"]
            status, answer = await call("memory_lessons.settings_put", {"auto_activate": True})
            assert status == 202, answer
    run_db(check, monkeypatch, timeout=60)


def test_push_channel_validation_and_confirmation(monkeypatch, tmp_path):
    async def check(pool):
        for name in ("synthetic-a", "synthetic-b"):
            await pool.execute("INSERT INTO channel_configs(channel_id,channel_type,enabled,config) "
                "VALUES ($1,'telegram',true,$2::jsonb)", name, json.dumps({"operator_sender_id": "123456"}))
        async with v1_api(tmp_path,pool) as call:
            status, answer = await call("memory_lessons.settings_put", {"weekly": {"push_channel": "missing"}})
            assert status == 400, answer
            status, answer = await call("memory_lessons.settings_put", {"weekly": {"push_channel": "synthetic-a"}})
            assert status == 200, answer
            status, answer = await call("memory_lessons.settings_put", {"enabled":True,"weekly":{"push_channel":"synthetic-a"}})
            assert status == 200, answer
            status, asked = await call("memory_lessons.settings_put", {"enabled":True,"weekly":{"push_channel":"synthetic-b"}})
            assert status == 202, asked
            status, answer = await call("confirm", {"confirmation_id": asked["confirmation_id"]})
            assert status == 200 and answer["result"]["settings"]["weekly"]["push_channel"] == "synthetic-b"
    run_db(check, monkeypatch, timeout=60)
