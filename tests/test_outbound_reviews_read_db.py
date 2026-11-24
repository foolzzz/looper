"""Bounded audit projections and stable, filter-bound pages on disposable PostgreSQL."""

import base64
from datetime import datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import api_v1
from looper.agents.ops import ErrorCode, OpError
from looper.agents.v1_domains import outbound_review
from looper.db import outbound_reviews
from tests.db_support import run_db


def _operation():
    return api_v1.find("POST", "/api/v1/outbound_review/list")


async def _page(**arguments):
    return await outbound_review.run(None, _operation(), arguments, api_v1.OPERATOR, {})


def _cursor(payload):
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def test_review_pages_filter_and_use_stable_cursor(monkeypatch):
    async def check(pool):
        await pool.execute("INSERT INTO projects (id, name) VALUES ('project-a', 'Synthetic'); "
                           "INSERT INTO tasks (id, requirement, project_id) VALUES ('task-a', 'A', 'project-a'), "
                           "('task-b', 'B', 'project-a')")
        timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index in range(103):
            await outbound_reviews.insert(agent_id="manager-synthetic", source="hook", tool="Bash",
                level="unknown", target_kind="command", target="echo sample", summary="synthetic fixture",
                verdict="block" if index % 3 else "allow", reason="fixture", task_id="task-a" if index % 2 else "task-b")
        await pool.execute("UPDATE outbound_reviews SET created_at = $1", timestamp)
        original = [str(row["id"]) for row in await pool.fetch("SELECT id FROM outbound_reviews ORDER BY id DESC")]
        first = await _page(limit=2)
        assert len(first["items"]) == 2 and first["next_cursor"]
        await outbound_reviews.insert(agent_id="new", source="tool", tool="sample", level="external",
            target_kind="system", target="synthetic", summary="later", verdict="allow", reason="later")
        found, page = [row["id"] for row in first["items"]], first
        while page["next_cursor"]:
            page = await _page(limit=2, cursor=page["next_cursor"])
            found.extend(row["id"] for row in page["items"])
        assert found == original and len(set(found)) == 103 and page["next_cursor"] is None
        filtered = await _page(verdict="block", task_id="task-a", limit=100)
        assert filtered["items"] and all(row["verdict"] == "block" and row["task_id"] == "task-a"
                                          for row in filtered["items"])
        assert await _page(task_id="missing") == {"items": [], "next_cursor": None}
        payload = {"v": 1, "created_at": timestamp.isoformat(), "id": original[1], "verdict": None,
                   "task_id": None}
        for changes in ({"v": 2}, {"v": True}, {"created_at": "2026-01-01T00:00:00"},
                        {"created_at": "bad"}, {"id": "0"}, {"id": "01"}, {"id": "9223372036854775808"},
                        {"id": 1}, {"extra": 1}, {"verdict": "block"}, {"task_id": "task-a"}):
            with pytest.raises(OpError) as error:
                await _page(cursor=_cursor({**payload, **changes}))
            assert error.value.code is ErrorCode.INVALID
        for cursor in ("bad", "!!!!", "", "x" * 2049):
            with pytest.raises(OpError):
                await _page(cursor=cursor)
        raw_payload = json.dumps(payload)
        for raw in ((raw_payload[:-1] + ',"v":1}').encode(), raw_payload.encode("utf-16"),
                    ("[" * 1000 + "0" + "]" * 1000).encode()):
            with pytest.raises(OpError) as error:
                await _page(cursor=base64.urlsafe_b64encode(raw).decode().rstrip("="))
            assert error.value.code is ErrorCode.INVALID
        calls = []

        async def fetch(query, *args):
            calls.append((query, args))
            return []

        await outbound_reviews.list_reviews(verdict="block", task_id="task-a", before=(timestamp, 5), limit=2,
                                           pool=SimpleNamespace(fetch=fetch))
        query, args = calls[0]
        assert "COUNT" not in query.upper() and "ORDER BY created_at DESC, id DESC" in query
        assert "(created_at, id) <" in query and "task-a" not in query and "block" not in query
        assert args[-1] == 3 and timestamp in args and 5 in args
    run_db(check, monkeypatch)


def test_review_projection_handles_nulls_and_does_not_expose_content(monkeypatch):
    async def check(pool):
        for failure in (None, "too_long", "protected"):
            await outbound_reviews.insert(agent_id="synthetic", source="post", tool="send", action="post",
                level="external", target_kind="chat", target="sample", summary="summary", verdict="block",
                reason="reason", failure=failure, signals=["operator_asked"], channel="private-channel",
                chat="private-chat", manager_action_id=7,
                parent_record_id=None if failure is None else 9007199254740993,
                record_id=None if failure is None else 9007199254740994,
                cost_usd=None if failure is None else Decimal("0.003001"),
                latency_ms=None if failure is None else 42)
        await pool.execute("UPDATE outbound_reviews SET id = id + 9007199254740992")
        page = await _page()
        assert len(page["items"]) == 3
        expected = {"id", "created_at", "agent_id", "task_id", "parent_record_id", "record_id", "source", "tool",
                    "action", "level", "target_kind", "target", "summary", "verdict", "failure", "reason",
                    "signals", "profile_id", "model", "cost_usd", "latency_ms"}
        for row in page["items"]:
            assert set(row) == expected
            assert isinstance(row["id"], str) and int(row["id"]) > 9007199254740992
            assert datetime.fromisoformat(row["created_at"]).tzinfo is not None
            assert row["task_id"] is None and row["profile_id"] is None
            if row["failure"] is None:
                assert row["cost_usd"] is None and row["latency_ms"] is None
                assert row["parent_record_id"] is None and row["record_id"] is None
            else:
                assert row["parent_record_id"] == "9007199254740993" and row["record_id"] == "9007199254740994"
                assert row["cost_usd"] == pytest.approx(0.003001) and type(row["cost_usd"]) is float
                assert row["latency_ms"] == 42
        assert "private-channel" not in json.dumps(page) and "private-chat" not in json.dumps(page)
        with monkeypatch.context() as patch:
            patch.setattr(outbound_reviews, "list_reviews", AsyncMock(side_effect=ConnectionError("offline")))
            with pytest.raises(OpError) as error:
                await _page()
            assert error.value.code is ErrorCode.UNAVAILABLE
        await pool.execute("UPDATE outbound_reviews SET cost_usd = 'NaN'")
        with pytest.raises(OpError) as error:
            await _page()
        assert error.value.code is ErrorCode.UNAVAILABLE
    run_db(check, monkeypatch)
