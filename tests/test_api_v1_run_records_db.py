"""Authenticated run projections on disposable PostgreSQL, including historical records and transcript cursors."""

import json
from contextlib import asynccontextmanager
from datetime import datetime

import pytest

from looper.agents import api_v1
from looper.db import api_clients, llm_records
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@asynccontextmanager
async def _api(tmp_path, pool, *, client=True):
    _, token = await api_clients.create("Run records", pool=pool)
    async with _serving(tmp_path, listen=True) as server:
        if client:
            status, _, answer = await _send(server, "GET /api/v1/whoami", _bearer(token))
            assert status == 200 and answer["result"]["principal"] == "client"
        async def call(action, body):
            path = f"/api/v1/runs/{action}"
            if client:
                status, _, answer = await _send(server, f"POST {path}", _bearer(token), json.dumps(body))
                assert status == 403 and answer["error"]["code"] == "forbidden"
            status, _, answer = await _send(server, f"POST {path}", _bearer(TOKEN),
                                             json.dumps(body))
            assert status == 200, answer
            assert answer["status"] == "ok"
            api_v1.validate_arguments(api_v1.find("POST", path).result, answer["result"])
            return answer["result"]
        yield call


async def _record(pool, agent="worker-one", task="task-one", *, at="2026-10-22T12:00:00Z", **values):
    rid = await llm_records.start_record(agent_id=agent, task_id=task, session_id=values.pop("session_id", None),
                                         input_text="Recorded prompt", metadata=values.pop("metadata", None), pool=pool)
    await llm_records.finish_record(rid, status=values.pop("status", "completed"), **values, pool=pool)
    await pool.execute("UPDATE llm_records SET created_at = $2 WHERE id = $1", rid, datetime.fromisoformat(at))
    return rid


@pytest.mark.parametrize("client", [True, False])
@pytest.mark.parametrize("filters,expected", [
    ({"task_id": "task-one"}, [2, 1, 0]),
    ({"agent_id": "worker-one"}, [3, 2, 0]),
    ({"role": "reviewer"}, [1]),
    ({"task_id": "task-one", "agent_id": "worker-one", "role": "worker", "limit": 1, "offset": 1}, [0]),
])
def test_list_filters_and_paging_read_the_matching_records(tmp_path, monkeypatch, client, filters, expected):
    async def run(pool):
        ids = [await _record(pool, agent, task, at=f"2026-10-22T12:00:0{i}Z",
                             cost_source="runtime" if i % 2 else "provider_price", cost_usd=0.1)
               for i, (agent, task) in enumerate((("worker-one", "task-one"), ("reviewer-one", "task-one"),
                                                  ("worker-one", "task-one"), ("worker-one", "task-two")))]
        async with _api(tmp_path, pool, client=client) as call:
            assert [row["id"] for row in (await call("list", filters))["records"]] == [ids[i] for i in expected]
    run_db(run, monkeypatch, timeout=30)


def test_task_rows_keep_profile_and_role_without_record_bodies(tmp_path, monkeypatch):
    async def run(pool):
        role = {"id": "specialist", "kind": "work", "version": 2, "role_prompt": "Recorded role prompt"}
        metadata = {"step_id": "step-one", "profile": {"name": "Example", "source": "project",
                    "runtime": {"type": "codex", "name": "Local"}, "model": "example-model"},
                    "model_choice": {"effort": "high"}, "prompt_layers": {"task_role": role}}
        rid = await _record(pool, metadata=metadata, output_text="Recorded output")
        await _record(pool, "manager", "task-one")
        await llm_records.append_events(rid, [(1, {"text": "Transcript"})], pool=pool)
        async with _api(tmp_path, pool) as call:
            (row,) = (await call("for_task", {"task_id": "task-one"}))["runs"]
            assert row["id"] == rid and row["role"] == "worker" and row["step_id"] == "step-one"
            assert row["profile"] == {"name": "Example", "source": "project"}
            assert row["runtime"] == {"type": "codex", "name": "Local"}
            assert row["model"] == "example-model" and row["effort"] == "high" and row["task_role"] == role
            assert not {"metadata", "input_text", "output_text", "prompt", "events"} & row.keys()
    run_db(run, monkeypatch, timeout=30)


def test_record_get_preserves_historical_metadata_and_interrupted_status(tmp_path, monkeypatch):
    async def run(pool):
        metadata = {"profile": {"name": "Legacy profile"}, "historical": {"nested": [None, {"value": 3}]},
                    "prompt_layers": {"task_role": {"id": "specialist", "kind": "work", "version": 1}}}
        rid = await _record(pool, status="interrupted", metadata=metadata, error="Stopped before completion")
        before = await llm_records.get_record(rid, pool=pool)
        async with _api(tmp_path, pool) as call:
            row = (await call("get", {"record_id": rid}))["record"]
            assert row["status"] == "interrupted" and row["metadata"] == llm_records.normalize_record_metadata(metadata)
            assert row["task_role"] == metadata["prompt_layers"]["task_role"]
            assert row["cost_source"] is None and row["context_tokens"] is None and row["compactions"] is None
            assert {key: row[key] for key in before} == before
        assert await llm_records.get_record(rid, pool=pool) == before
    run_db(run, monkeypatch, timeout=30)


def test_event_cursor_keeps_every_nested_event_and_never_repeats_a_sequence(tmp_path, monkeypatch):
    async def run(pool):
        rid = await _record(pool)
        events = [(seq, {"type": "assistant", "message": {"content": [{"type": "tool_use", "input": {
            "seq": seq, "values": [None, True, {"text": "Transcript detail"}]}}]}, "extra": {"future": seq}})
                  for seq in (1, 3, 7, 9)]
        await llm_records.append_events(rid, list(reversed(events)), pool=pool)
        async with _api(tmp_path, pool) as call:
            rows, after = [], 0
            for _ in range(3):
                page = (await call("events", {"record_id": rid, "after": after, "limit": 2}))["events"]
                if page:
                    assert all(row["seq"] > after for row in page)
                    after = page[-1]["seq"]
                rows.extend(page)
            assert [(row["seq"], row["event"]) for row in rows] == events
            assert all(set(row) == {"seq", "event", "created_at"} for row in rows)
            assert (await call("events", {"record_id": rid + 100}))["events"] == []
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("cost,context,compactions", [(0.25, 120, [{"trigger": "auto", "extra": [1]}]),
                                                      (None, None, None), (0.0, 0, [])])
def test_usage_totals_session_and_utc_days_preserve_unpriced_and_null_values(
        tmp_path, monkeypatch, cost, context, compactions):
    async def run(pool):
        first = await _record(pool, "planner-one", session_id="session-one", cost_usd=cost,
                              context_tokens=context, compactions=compactions, at="2026-10-22T23:30:00-07:00")
        second = await _record(pool, "planner-one", status="running", at="2026-10-24T00:30:00Z")
        await _record(pool, "worker-other", cost_usd=100)
        async with _api(tmp_path, pool) as call:
            usage = await call("usage", {"agent_id": "planner-one"})
            assert usage["total"] == {"cost_usd": cost or 0, "runs": 2, "running": 1,
                                      "unpriced_runs": int(cost is None), "compactions": len(compactions or [])}
            assert [(row["session_id"], row["cost_usd"], row["runs"]) for row in usage["by_session"]] == [
                (None, 0, 1), ("session-one", cost or 0, 1)]
            assert usage["by_day"] == [{"day": "2026-10-24", "cost_usd": 0, "runs": 1},
                                       {"day": "2026-10-23", "cost_usd": cost or 0, "runs": 1}]
            assert usage["by_session"][1]["first_at"] == usage["by_session"][1]["last_at"] == "2026-10-23T06:30:00+00:00"
            assert [row["id"] for row in usage["turns"]] == [first, second]
            assert usage["turns"][0]["compactions"] == compactions
            assert usage["turns"][0]["context_tokens"] == context
            assert usage["turns"][1]["session_id"] is None and usage["turns"][1]["cost_usd"] is None
            assert usage["latest_context_tokens"] == context
            limited = await call("usage", {"agent_id": "planner-one", "turns": 1})
            assert [row["id"] for row in limited["turns"]] == [second]
            assert limited["latest_context_tokens"] is None and limited["total"] == usage["total"]
    run_db(run, monkeypatch, timeout=30)
