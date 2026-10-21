"""Paged task activity and conversation through the HTTP handler, against a disposable PostgreSQL."""

from __future__ import annotations

from typing import Any

import pytest

from looper.db import activity
from tests.test_ops_tasks_db import _project, _task
from tests.test_usage_api_db import _get, _run


async def _fill(pool: Any, task_id: str, count: int, *, kind: str = "tool") -> list[int]:
    return [await activity.insert_activity(task_id=task_id, kind=kind, summary=f"row {i}", pool=pool)
            for i in range(count)]


def test_without_a_cursor_the_latest_window_comes_oldest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "running", await _project(pool))
        seqs = await _fill(pool, "task-a", 25)

        status, body = await _get("/api/tasks/task-a/activity", {"limit": "10"})
        assert status == 200
        assert [r["seq"] for r in body["activity"]] == seqs[-10:]
        assert (body["has_more"], body["oldest_seq"], body["latest_seq"]) == (True, seqs[15], seqs[-1])

        status, body = await _get("/api/tasks/task-a/activity", {"limit": "10", "before": str(seqs[15])})
        assert [r["seq"] for r in body["activity"]] == seqs[5:15]
        assert (body["has_more"], body["oldest_seq"], body["latest_seq"]) == (True, seqs[5], seqs[-1])

        status, body = await _get("/api/tasks/task-a/activity", {"limit": "10", "before": str(seqs[5])})
        assert [r["seq"] for r in body["activity"]] == seqs[:5]
        assert (body["has_more"], body["oldest_seq"]) == (False, seqs[0])

        status, body = await _get("/api/tasks/task-a/activity", {"limit": "25"})
        assert [r["seq"] for r in body["activity"]] == seqs and body["has_more"] is False

        status, body = await _get("/api/tasks/task-a/activity", {"before": str(seqs[0])})
        assert body["activity"] == [] and body["has_more"] is False and body["oldest_seq"] is None

    _run(test, monkeypatch)


def test_an_empty_task_and_other_tasks_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _task(pool, "task-b", "running", project)
        await _fill(pool, "task-b", 3)

        status, body = await _get("/api/tasks/task-a/activity")
        assert status == 200
        assert body["activity"] == [] and body["has_more"] is False
        assert body["oldest_seq"] is None and body["latest_seq"] is None
        status, body = await _get("/api/tasks/task-a/conversation")
        assert status == 200 and body["conversation"] == [] and body["has_more"] is False

    _run(test, monkeypatch)


def test_the_window_is_capped_and_defaults_to_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "running", await _project(pool))
        await pool.execute(
            "INSERT INTO activity (task_id, worker, kind, summary) "
            "SELECT 'task-a', '', 'tool', 'r' || g FROM generate_series(1, 1005) g")
        top = await pool.fetchval("SELECT max(seq) FROM activity")

        for query in ({}, {"limit": "5000"}):
            status, body = await _get("/api/tasks/task-a/activity", query)
            assert status == 200 and len(body["activity"]) == 1000 and body["has_more"] is True
            assert body["activity"][-1]["seq"] == top
            assert [r["seq"] for r in body["activity"]] == sorted(r["seq"] for r in body["activity"])
        status, body = await _get("/api/tasks/task-a/conversation", {"limit": "5000"})
        assert body["conversation"] == []

    _run(test, monkeypatch)


def test_forward_reads_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "running", await _project(pool))
        seqs = await _fill(pool, "task-a", 6)

        for name in ("since", "after"):
            status, body = await _get("/api/tasks/task-a/activity", {name: str(seqs[1]), "limit": "3"})
            assert status == 200
            assert [r["seq"] for r in body["activity"]] == seqs[2:5]
            assert "has_more" not in body
        status, body = await _get("/api/tasks/task-a/activity", {"since": "0"})
        assert [r["seq"] for r in body["activity"]] == seqs

    _run(test, monkeypatch)


def test_the_conversation_is_windowed_like_the_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "running", await _project(pool))
        talk: list[int] = []
        for i in range(12):
            await activity.insert_activity(task_id="task-a", kind="tool", summary="noise", pool=pool)
            talk.append(await activity.insert_activity(
                task_id="task-a", kind=("text", "phase", "error", "user_message")[i % 4], summary=f"t{i}", pool=pool))

        status, body = await _get("/api/tasks/task-a/conversation", {"limit": "5"})
        assert status == 200
        assert [r["seq"] for r in body["conversation"]] == talk[-5:]
        assert (body["has_more"], body["oldest_seq"], body["latest_seq"]) == (True, talk[7], talk[-1])

        status, body = await _get("/api/tasks/task-a/conversation", {"limit": "5", "before": str(talk[7])})
        assert [r["seq"] for r in body["conversation"]] == talk[2:7] and body["has_more"] is True

        status, body = await _get("/api/tasks/task-a/conversation", {"limit": "5", "before": str(talk[2])})
        assert [r["seq"] for r in body["conversation"]] == talk[:2]
        assert body["has_more"] is False and body["oldest_seq"] == talk[0]

        status, body = await _get("/api/tasks/task-a/conversation")
        assert [r["seq"] for r in body["conversation"]] == talk and body["has_more"] is False

    _run(test, monkeypatch)


def test_a_bad_cursor_is_a_400(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "running", await _project(pool))
        for path in ("activity", "conversation"):
            status, _ = await _get(f"/api/tasks/task-a/{path}", {"before": "x"})
            assert status == 400

    _run(test, monkeypatch)
