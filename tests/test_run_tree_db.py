"""Task-scoped run trees, truthful costs, waits and operator-only access."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx

import pytest

from looper.agents import api_v1, run_tree
from looper.db import agent_spawns, llm_records
from looper.db.agent_spawns import SpawnState
from tests.db_support import DSN, run_db
from looper.agents.http_server import AgentHTTPServer

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _task(pool, task_id="tree-task"):
    await pool.execute("INSERT INTO projects(id,name) VALUES ('tree-project','Run tree') ON CONFLICT DO NOTHING")
    await pool.execute("INSERT INTO tasks(id,project_id,requirement,status) VALUES ($1,'tree-project','Inspect code','init')", task_id)


async def _record(pool, kind, *, parent=None, cost=.1, equivalent=False, task="tree-task", metadata=None):
    return await pool.fetchval("""INSERT INTO llm_records(agent_id,task_id,run_kind,parent_record_id,
        status,cost_usd,model,metadata,created_at,finished_at,output_summary)
        VALUES ($1,$2,$3,$4,'completed',$5,'example-model',$6::jsonb,
            '2026-11-22T12:00:00Z','2026-11-22T12:00:10Z','Finished investigation') RETURNING id""",
        f"{kind}-example", task, kind, parent, cost, json.dumps({"accounting_basis":
        "subscription_equivalent" if equivalent else "provider_price", **(metadata or {})}))


async def _spawn(pool, parent, child, *, cancelled=False):
    row = await agent_spawns.create(kind="explorer", task_id="tree-task", step_id=None,
        parent_record_id=parent, brief={"goal": "Inspect the scheduler"}, inputs={}, budget_usd=.5,
        state=SpawnState.QUEUED, spawner="worker@s1", delivery="tool", parent_spend_known=False, pool=pool)
    await pool.execute("""UPDATE agent_spawns SET child_record_id=$2,state=$3,finished_at=now(),
        cancel_reason=$4,result=$5::jsonb,spent_usd=.07 WHERE id=$1""", row["id"], child,
        "cancelled" if cancelled else "answered", "parent_ended" if cancelled else None,
        json.dumps({"summary": "s" * 350, "verified": not cancelled}))


def test_tree_includes_every_run_and_sums_descendants_without_double_counting(monkeypatch):
    async def check(pool):
        await _task(pool)
        planner = await _record(pool, "planner", cost=.3, equivalent=True)
        worker = await _record(pool, "worker", cost=.2, metadata={"profile": {"provider": {"auth": "oauth"}}})
        reviewer = await _record(pool, "reviewer", cost=.1)
        await pool.execute("UPDATE llm_records SET run_kind=NULL WHERE id=$1", reviewer)
        advisor = await _record(pool, "advisor", parent=planner, cost=.05, equivalent=True)
        consult = await _record(pool, "consult", parent=worker, cost=.02)
        cancelled = await _record(pool, "explorer", parent=worker, cost=.04)
        answered = await _record(pool, "explorer", parent=worker, cost=.03, equivalent=True,
                                 metadata={"profile": {"model": "profile-model"}})
        await pool.execute("UPDATE llm_records SET model=NULL WHERE id=$1", answered)
        await _spawn(pool, worker, cancelled, cancelled=True)
        await _spawn(pool, worker, answered)
        await _task(pool, "other-task")
        outsider = await _record(pool, "explorer", parent=worker, task="other-task", cost=100)
        result = await run_tree.read("tree-task", pool=pool)
        nodes = {node["id"]: node for node in result["nodes"]}
        assert set(nodes) == {planner, worker, reviewer, advisor, consult, cancelled, answered}
        assert outsider not in nodes
        assert nodes[planner]["subtree_cost"] == {"subscription_usd": .35, "api_usd": 0}
        assert nodes[worker]["own_cost"] == {"subscription_usd": 0, "api_usd": .2}
        assert nodes[worker]["subtree_cost"] == {"subscription_usd": .03, "api_usd": .26}
        child = nodes[cancelled]
        assert child["parent_record_id"] == worker
        assert child["spawner"] == {"role": "worker", "seat": "worker@s1"}
        assert child["reason"] == "Inspect the scheduler"
        assert child["result_summary"] == "s" * 300 and child["result_verified"] is False
        assert child["state"] == "cancelled" and child["cancel_reason"] == "parent_ended"
        assert child["delivery"] == "tool" and child["parent_spend_known"] is False
        assert nodes[reviewer]["reason"] == "reviewer"
        assert nodes[consult]["kind"] == "consult"
        assert nodes[answered]["model"] == "profile-model"
        run_rows = await llm_records.task_run_rows("tree-task", pool=pool)
        assert {row["id"] for row in run_rows if row["role"] == "explorer"} == {cancelled, answered}
        schema = api_v1.find("POST", "/api/v1/runs/for_task").result
        api_v1.validate_arguments(schema, {"runs": run_rows})
    run_db(check, monkeypatch)


def test_unknown_cost_propagates_only_to_its_billing_basis_and_preserves_failures():
    async def check(pool):
        await _task(pool)
        parent = await _record(pool, "worker", cost=.2)
        child = await _record(pool, "explorer", parent=parent, cost=None, equivalent=True)
        await pool.execute("UPDATE llm_records SET status='failed',failure_class='judgment',failure_cause='timeout' WHERE id=$1", child)
        nodes = {node["id"]: node for node in (await run_tree.read("tree-task", pool=pool))["nodes"]}
        assert nodes[child]["own_cost"] == {"subscription_usd": None, "api_usd": 0}
        assert nodes[parent]["subtree_cost"] == {"subscription_usd": None, "api_usd": .2}
        assert nodes[child]["failure_class"] == "judgment" and nodes[child]["failure_cause"] == "timeout"
    run_db(check)


def test_waits_pair_tool_ids_and_keep_an_unfinished_wait_open():
    async def check(pool):
        await _task(pool)
        parent = await _record(pool, "worker")
        blocks = [
            ("assistant", {"type": "tool_use", "id": "wait-one", "name": "mcp__looper__looper_agent_wait"}),
            ("assistant", {"type": "tool_use", "id": "read-one", "name": "Read"}),
            ("user", {"type": "tool_result", "tool_use_id": "read-one"}),
            ("user", {"type": "tool_result", "tool_use_id": "wait-one"}),
            ("assistant", {"type": "tool_use", "id": "wait-open", "name": "looper_agent_wait"}),
        ]
        for seq, (kind, block) in enumerate(blocks):
            await pool.execute("INSERT INTO llm_record_events(record_id,seq,event,created_at) VALUES ($1,$2,$3::jsonb,$4)",
                parent, seq, json.dumps({"type": kind, "message": {"content": [block]}}),
                datetime(2026, 11, 22, 12, 0, seq, tzinfo=timezone.utc))
        await pool.execute("INSERT INTO llm_record_events(record_id,seq,event) VALUES ($1,9,$2::jsonb)",
            parent, json.dumps({"type": "assistant", "parent_tool_use_id": "native-child",
                "message": {"content": [{"type": "tool_use", "id": "native-wait", "name": "looper_agent_wait"}]}}))
        waits = (await run_tree.read("tree-task", pool=pool))["nodes"][0]["wait_intervals"]
        assert waits == [{"started_at": "2026-11-22T12:00:00+00:00", "ended_at": "2026-11-22T12:00:03+00:00"},
                         {"started_at": "2026-11-22T12:00:04+00:00", "ended_at": None}]
    run_db(check)


def test_tree_read_does_not_change_task_costs_or_add_an_unused_explorer_share():
    async def check(pool):
        await _task(pool)
        await _record(pool, "planner", cost=.2)
        await _record(pool, "worker", cost=.1)
        before = json.dumps(await llm_records.task_costs(["tree-task"], pool=pool), sort_keys=True)
        assert set(json.loads(before)["tree-task"]["by_role"]) == {"planner", "worker", "reviewer"}
        await run_tree.read("tree-task", pool=pool)
        after = json.dumps(await llm_records.task_costs(["tree-task"], pool=pool), sort_keys=True)
        assert after == before
    run_db(check)


def test_tree_is_operator_only_and_returns_a_documented_schema(tmp_path, monkeypatch):
    from looper.db import api_clients

    async def check(pool):
        await _task(pool)
        await _record(pool, "worker")
        _, token = await api_clients.create("Run tree client", pool=pool)
        operator = "operator_" + "a" * 32
        server = AgentHTTPServer(host="127.0.0.1", port=0, operator_token=operator,
            static_dir=tmp_path, runtime_root=tmp_path, web_adapter=SimpleNamespace(),
            task_router=SimpleNamespace(), supervisor=SimpleNamespace())
        await server.start()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server._port}", trust_env=False) as client:
                answer = await client.post("/api/v1/runs/tree", headers={"Authorization": f"Bearer {token}"},
                                           json={"task_id": "tree-task"})
                assert answer.status_code == 403 and answer.json()["error"]["code"] == "forbidden"
                answer = await client.post("/api/v1/runs/tree", headers={"Authorization": f"Bearer {operator}"},
                                           json={"task_id": "tree-task"})
                assert answer.status_code == 200 and len(answer.json()["result"]["nodes"]) == 1
                api_v1.validate_arguments(api_v1.find("POST", "/api/v1/runs/tree").result, answer.json()["result"])
        finally:
            await server.stop()
    run_db(check, monkeypatch, timeout=30)


def test_empty_tree_and_cross_task_cycles_are_bounded():
    async def check(pool):
        await _task(pool)
        assert await run_tree.read("tree-task", pool=pool) == {"task_id": "tree-task", "nodes": []}
        one = await _record(pool, "worker", cost=.1)
        two = await _record(pool, "explorer", parent=one, cost=.2)
        await pool.execute("UPDATE llm_records SET parent_record_id=$2 WHERE id=$1", one, two)
        nodes = (await run_tree.read("tree-task", pool=pool))["nodes"]
        assert len(nodes) == 2
        assert all(node["subtree_cost"]["api_usd"] == .3 for node in nodes)
    run_db(check)
