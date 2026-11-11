"""Worker activity is isolated by task and worker, bounded, and chronological."""

import pytest

from looper.db import activity
from tests.db_support import run_db


def test_worker_activity_returns_the_newest_rows_without_detail():
    async def test(pool):
        await pool.execute("INSERT INTO projects (id, name) VALUES ('project', 'Project')")
        await pool.execute("INSERT INTO tasks (id, requirement, project_id) VALUES "
                           "('task', 'Inspect', 'project'), ('other', 'Other task', 'project')")
        await pool.execute("INSERT INTO activity (task_id, worker, kind, summary) "
                           "SELECT 'task', 'a', 'tool', 'old ' || n FROM generate_series(1, 450) n")
        seqs = [await activity.insert_activity(
            task_id="task", worker="b", kind="tool", summary=f"tool {i}", detail="full output",
            metadata={"tool": "Read"}, input_tokens=2, output_tokens=3, pool=pool,
        ) for i in range(10)]
        await activity.insert_activity(task_id="other", worker="b", kind="tool", summary="other task", pool=pool)

        rows = await activity.list_worker_activity("task", "b", limit=6, pool=pool)

        assert [row["seq"] for row in rows] == seqs[-6:]
        assert [row["summary"] for row in rows] == [f"tool {i}" for i in range(4, 10)]
        assert all(row["task_id"] == "task" and row["worker"] == "b" for row in rows)
        assert all("detail" not in row and row["created_at"] is not None for row in rows)
        assert all(row["metadata"] and (row["input_tokens"], row["output_tokens"]) == (2, 3) for row in rows)
        assert [row["seq"] for row in await activity.list_worker_activity("task", "b", pool=pool)] == seqs[-6:]
        assert await activity.list_worker_activity("task", "missing", pool=pool) == []

    run_db(test, timeout=30)


@pytest.mark.parametrize("limit,expected", [(-1, 1), (0, 1), (50, 50), (51, 50)])
def test_worker_activity_clamps_the_limit(limit, expected):
    async def test(pool):
        await pool.execute("INSERT INTO projects (id, name) VALUES ('project', 'Project')")
        await pool.execute("INSERT INTO tasks (id, requirement, project_id) VALUES ('task', 'Inspect', 'project')")
        await pool.execute("INSERT INTO activity (task_id, worker, kind, summary) "
                           "SELECT 'task', 'b', 'tool', 'row ' || n FROM generate_series(1, 55) n")
        rows = await activity.list_worker_activity("task", "b", limit=limit, pool=pool)
        assert len(rows) == expected
        assert rows[0]["summary"] == f"row {56 - expected}"
        assert rows[-1]["summary"] == "row 55"

    run_db(test, timeout=30)
