"""Paged task activity and conversation through the HTTP handler, against a disposable PostgreSQL."""

from __future__ import annotations

import json
from typing import Any

import pytest

from looper.agents import api_v1
from looper.agents.http_server import AgentHTTPServer
from looper.db import activity
from tests.db_support import run_db
from tests.test_api_v1_task_activity import validate_result
from tests.test_ops_tasks_db import _project, _task


async def _get(path: str, query: dict[str, str] | None = None) -> tuple[int, Any]:
    task_id, action = path.split("/")[-2:]
    body = {"task_id": task_id, **{k: int(v) if v.lstrip("-").isdigit() else v
                                 for k, v in (query or {}).items()}}
    server = AgentHTTPServer(web_adapter=None, supervisor=None, task_router=None)
    op = api_v1.find("POST", f"/api/v1/tasks/{action}")
    status, answer = await server._answer_v1("POST", op.path, op, {}, json.dumps(body).encode(), api_v1.OPERATOR)
    if "result" in answer:
        validate_result(op.result, answer["result"])
    return int(status.split()[0]), answer.get("result", answer)


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

    run_db(test, monkeypatch)


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
        assert status == 200 and body["rows"] == [] and body["has_more"] is False

    run_db(test, monkeypatch)


def test_the_window_is_capped_and_defaults_to_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "running", await _project(pool))
        await pool.execute(
            "INSERT INTO activity (task_id, worker, kind, summary) "
            "SELECT 'task-a', '', 'text', 'r' || g FROM generate_series(1, 1005) g")
        top = await pool.fetchval("SELECT max(seq) FROM activity")

        for query in ({}, {"limit": "5000"}):
            status, body = await _get("/api/tasks/task-a/activity", query)
            assert status == 200 and len(body["activity"]) == 1000 and body["has_more"] is True
            assert body["activity"][-1]["seq"] == top
            assert [r["seq"] for r in body["activity"]] == sorted(r["seq"] for r in body["activity"])
        status, body = await _get("/api/tasks/task-a/conversation", {"limit": "5000"})
        assert len(body["rows"]) == 1000 and body["has_more"] is True
        for query, expected in (({"after": "0"}, 100), ({"after": "0", "limit": "5000"}, 500),
                                ({"after": "0", "limit": "0"}, 0), ({"limit": "0"}, 1), ({"limit": "-5"}, 1)):
            status, body = await _get("/api/tasks/task-a/activity", query)
            assert status == 200 and len(body["activity"]) == expected

    run_db(test, monkeypatch)


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
        status, body = await _get("/api/tasks/task-a/activity", {
            "after": str(seqs[1]), "since": "0", "before": str(seqs[0]), "limit": "3"})
        assert [r["seq"] for r in body["activity"]] == seqs[2:5]

    run_db(test, monkeypatch)


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
        assert [r["seq"] for r in body["rows"]] == talk[-5:]
        assert (body["has_more"], body["oldest_seq"], body["latest_seq"]) == (True, talk[7], talk[-1])

        status, body = await _get("/api/tasks/task-a/conversation", {"limit": "5", "before": str(talk[7])})
        assert [r["seq"] for r in body["rows"]] == talk[2:7] and body["has_more"] is True

        status, body = await _get("/api/tasks/task-a/conversation", {"limit": "5", "before": str(talk[2])})
        assert [r["seq"] for r in body["rows"]] == talk[:2]
        assert body["has_more"] is False and body["oldest_seq"] == talk[0]

        status, body = await _get("/api/tasks/task-a/conversation")
        assert [r["seq"] for r in body["rows"]] == talk and body["has_more"] is False
        assert set(body["rows"][0]) == {"seq", "worker", "kind", "summary", "created_at"}

    run_db(test, monkeypatch)


def test_a_bad_cursor_is_a_400(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "running", await _project(pool))
        for path in ("activity", "conversation"):
            status, _ = await _get(f"/api/tasks/task-a/{path}", {"before": "x"})
            assert status == 400

    run_db(test, monkeypatch)


def test_detail_matches_task_and_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _task(pool, "task-b", "running", project)
        seq = await activity.insert_activity(task_id="task-a", kind="text", detail="Public example", metadata={"public": True}, pool=pool)
        status, body = await _get("/api/tasks/task-a/activity_detail", {"seq": str(seq)})
        assert status == 200 and body["detail"] == "Public example" and body["task_id"] == "task-a"
        assert body["metadata"] == {"public": True}
        for task_id, value in (("task-b", seq), ("task-a", seq + 1000), ("missing-task", seq)):
            status, body = await _get(f"/api/tasks/{task_id}/activity_detail", {"seq": str(value)})
            assert status == 404 and body["error"]["code"] == "not_found"
    run_db(test, monkeypatch, timeout=30)
