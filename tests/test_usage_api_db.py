"""The cost and context read-back API against a real PostgreSQL.

Runs only when LOOPX_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

DSN = os.environ.get("LOOPX_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPX_TEST_DATABASE_URL is not set")


def _run(test: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run ``test(pool)`` on a new schema that is also the server's pool."""
    import asyncpg

    from loopx.db import connection

    async def _main() -> None:
        schema = f"loopx_test_{uuid.uuid4().hex[:12]}"
        admin = await asyncpg.connect(DSN)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3, server_settings={"search_path": schema})
        try:
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "_pool", pool)
            await test(pool)
        finally:
            await pool.close()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            await admin.close()

    asyncio.run(_main())


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


async def _get(path: str, query: dict[str, str] | None = None) -> tuple[int, Any]:
    from loopx.agents.http_server import AgentHTTPServer
    from loopx.agents.tiers.task import TaskRouter

    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())))
    writer = _Writer()
    await server._route_api("GET", path, query or {}, b"", writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


async def _run_record(pool: Any, agent_id: str, *, task_id: str | None = None, cost: float | None = None,
                      session_id: str | None = None, context: int | None = None,
                      compactions: list[dict[str, Any]] | None = None) -> int:
    from loopx.db import llm_records

    record_id = await llm_records.start_record(agent_id=agent_id, task_id=task_id, session_id=session_id, pool=pool)
    await llm_records.finish_record(record_id, status="completed", cost_usd=cost, input_tokens=10, output_tokens=20,
                                    cache_read_tokens=30, cache_creation_tokens=40, context_tokens=context,
                                    compactions=compactions or [], pool=pool)
    return record_id


async def _task(pool: Any, task_id: str, project_id: str) -> None:
    from loopx.db import tasks

    await tasks.create_task(task_id=task_id, requirement=f"do {task_id}", project_id=project_id, pool=pool)


def test_the_task_detail_and_list_carry_the_tasks_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    from loopx.db import llm_records, projects

    async def test(pool: Any) -> None:
        project = await projects.create_project(name="calc", pool=pool)
        await _task(pool, "task-a", project["id"])
        await _task(pool, "task-b", project["id"])
        for agent_id, cost in (("planner-task-a", 0.12), ("planner-task-a", 0.03), ("worker-1", 0.4),
                               ("reviewer-2", 0.05), ("worker-3", 0.25)):
            await _run_record(pool, agent_id, task_id="task-a", cost=cost)
        await _run_record(pool, "manager", task_id="task-a", cost=7.0)

        status, detail = await _get("/api/tasks/task-a")
        assert status == 200
        cost = detail["cost"]
        runs = [r for r in await llm_records.list_records(task_id="task-a", pool=pool) if r["agent_id"] != "manager"]
        assert len(runs) == cost["runs"] == 5
        assert cost["cost_usd"] == pytest.approx(sum(r["cost_usd"] for r in runs)) == pytest.approx(0.85)
        assert {role: (pytest.approx(row["cost_usd"]), row["runs"]) for role, row in cost["by_role"].items()} == {
            "planner": (0.15, 2), "worker": (0.65, 2), "reviewer": (0.05, 1)}
        assert (cost["running"], cost["unpriced_runs"]) == (0, 0)

        status, listed = await _get("/api/tasks")
        assert status == 200
        by_id = {task["task_id"]: task for task in listed["tasks"]}
        assert by_id["task-a"]["cost"] == cost
        assert by_id["task-b"]["cost"]["cost_usd"] == 0.0 and by_id["task-b"]["cost"]["runs"] == 0

    _run(test, monkeypatch)


def test_an_agents_usage_reads_back_its_context_cost_and_compactions(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        compacted = [{"trigger": "auto", "pre_tokens": 25120, "post_tokens": 1359, "duration_ms": 13135}]
        await _run_record(pool, "manager", session_id="s1", cost=0.25, context=18000)
        await _run_record(pool, "manager", session_id="s1", cost=0.5, context=24000)
        await _run_record(pool, "manager", session_id="s1", cost=0.75, context=17000, compactions=compacted)

        status, usage = await _get("/api/agents/manager/usage")
        assert status == 200
        assert usage["total"]["cost_usd"] == pytest.approx(1.5)
        assert (usage["total"]["runs"], usage["total"]["compactions"]) == (3, 1)
        assert [t["context_tokens"] for t in usage["turns"]] == [18000, 24000, 17000]
        assert usage["turns"][-1]["compactions"] == compacted
        assert usage["latest_context_tokens"] == 17000
        assert [(s["session_id"], s["runs"]) for s in usage["by_session"]] == [("s1", 3)]
        assert len(usage["by_day"]) == 1

        status, latest = await _get("/api/agents/manager/usage", {"turns": "1"})
        assert status == 200 and [t["context_tokens"] for t in latest["turns"]] == [17000]
        status, bad = await _get("/api/agents/manager/usage", {"turns": "many"})
        assert status == 400 and "turns" in bad["error"]

    _run(test, monkeypatch)
