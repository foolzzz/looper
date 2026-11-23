"""The metrics and task-feedback operations on disposable PostgreSQL and the authenticated v1 API."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from looper.agents import api_v1, metrics
from looper.db import llm_records
from tests.db_support import DSN, run_db, v1_api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

NOTE = "SENTINEL-feedback-note-for-the-operator-only"


async def seed(pool):
    await pool.execute("INSERT INTO projects(id,name) VALUES ('p','Synthetic project'),('q','Other project')")


async def task(pool, task_id, project="p", status="completed"):
    await pool.execute("INSERT INTO tasks(id,requirement,project_id,status) VALUES ($1,'Synthetic task',$2,$3)",
                       task_id, project, status)


async def outcome(pool, task_id, ended_ago, *, run_no=1, result="completed", cause="planner_end", kind=None,
                  minutes=10, usd=1.0, unpriced=0):
    ended = datetime.now(timezone.utc) - ended_ago
    await pool.execute(
        "INSERT INTO task_outcomes(task_id,run_no,outcome,stop_cause,failure_kind,started_at,ended_at,"
        "provider_priced_usd,equivalent_usd,unpriced_runs) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,0,$9)",
        task_id, run_no, result, cause, kind, ended - timedelta(minutes=minutes), ended, usd, unpriced)


async def attempt(pool, task_id, step, n, trigger, decision, ago, **columns):
    started = datetime.now(timezone.utc) - ago - timedelta(minutes=5)
    row = {"task_id": task_id, "step_key": step, "task_run_no": 1, "attempt": n, "trigger": trigger,
           "decision": decision, "started_at": started,
           "decided_at": started + timedelta(minutes=5) if decision else None, "worker_runs": 1, **columns}
    names = ",".join(row)
    return await pool.fetchval(
        f"INSERT INTO step_attempts({names}) VALUES ({','.join(f'${i + 1}' for i in range(len(row)))}) RETURNING id",
        *row.values())


async def record(pool, agent, task_id, *, cost=None, parent=None, attempt_id=None, kind=None, metadata=None,
                 equivalent=False, ago=timedelta(hours=1)):
    meta = {**(metadata or {}), **({"accounting_basis": "subscription_equivalent"} if equivalent else {})}
    return await pool.fetchval(
        "INSERT INTO llm_records(agent_id,task_id,cost_usd,parent_record_id,attempt_id,run_kind,metadata,created_at) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8) RETURNING id",
        agent, task_id, cost, parent, attempt_id, kind, json.dumps(meta), datetime.now(timezone.utc) - ago)


def test_operations_are_operator_only_reads_with_short_summaries_and_levels():
    for name in ("metrics.task_outcomes", "metrics.step_attempts", "task_feedback.get", "task_feedback.set"):
        operation = api_v1.find("POST", "/api/v1/" + name.replace(".", "/"))
        assert operation.operator_only and operation.tool is None and len(operation.summary) <= 120
        assert operation.level.value == "inside"
        assert operation.risk == ("write" if name == "task_feedback.set" else "read")
    from looper.agents.ops.service import MANAGER_TOOLS
    assert all("task_feedback" not in tool.name and "metrics" not in tool.name for tool in MANAGER_TOOLS)


def test_a_client_token_cannot_read_metrics_or_mark_feedback(monkeypatch, tmp_path):
    async def check(pool):
        await task(pool, "t1")
        async with v1_api(tmp_path, pool) as call:
            for action, body in (("metrics.task_outcomes", {}), ("metrics.step_attempts", {"task_id": "t1"}),
                                 ("task_feedback.get", {"task_id": "t1"}),
                                 ("task_feedback.set", {"task_id": "t1", "verdict": "defect"})):
                status, answer = await call(action, body, client=True)
                assert status == 403 and answer["error"]["code"] == "forbidden", action
            assert await pool.fetchval("SELECT count(*) FROM task_feedback") == 0
    run_db(check, monkeypatch, setup=seed)


def test_feedback_is_set_changed_withdrawn_and_refused_unless_completed(monkeypatch, tmp_path):
    async def check(pool):
        await task(pool, "done")
        await task(pool, "running", status="running")
        async with v1_api(tmp_path, pool) as call:
            assert (await call("task_feedback.get", {"task_id": "done"}))[1]["result"]["verdict"] is None
            status, body = await call("task_feedback.set", {"task_id": "done", "verdict": "defect", "note": NOTE})
            assert status == 200 and body["result"]["verdict"] == "defect" and body["result"]["note"] == NOTE
            created = body["result"]["created_at"]
            status, body = await call("task_feedback.set", {"task_id": "done", "verdict": "wrong_approach"})
            assert body["result"]["verdict"] == "wrong_approach" and body["result"]["note"] is None
            assert body["result"]["created_at"] == created and body["result"]["updated_at"] >= created
            assert (await call("task_feedback.get", {"task_id": "done"}))[1]["result"]["verdict"] == "wrong_approach"
            status, body = await call("task_feedback.set", {"task_id": "done", "verdict": None})
            assert status == 200 and body["result"]["verdict"] is None
            assert await pool.fetchval("SELECT count(*) FROM task_feedback") == 0
            status, body = await call("task_feedback.set", {"task_id": "running", "verdict": "defect"})
            assert status == 409 and body["error"]["code"] == "conflict"
            assert (await call("task_feedback.set", {"task_id": "nope", "verdict": "defect"}))[0] == 404
            for bad in ({"verdict": "bad"}, {"verdict": "defect", "note": "x" * 501}, {"note": "x"}):
                assert (await call("task_feedback.set", {"task_id": "done", **bad}))[0] == 400
            assert (await call("task_feedback.set", {"task_id": "done", "verdict": "other", "note": "x" * 500}))[0] == 200
            # The operator's mark is not an action of the Manager's or a client's: neither it nor its note is kept
            # in the action trail.
            assert NOTE not in json.dumps([dict(r) for r in await pool.fetch("SELECT * FROM manager_actions")], default=str)
    run_db(check, monkeypatch, setup=seed)


def test_feedback_note_reaches_no_agent_context(monkeypatch, tmp_path):
    from looper.agents import projects
    from looper.agents.tiers.planner import PlannerAgent, PlanStep, TaskPlan

    async def check(pool):
        await task(pool, "done")
        async with v1_api(tmp_path, pool) as call:
            assert (await call("task_feedback.set", {"task_id": "done", "verdict": "defect", "note": NOTE}))[0] == 200
        planner = PlannerAgent(task_id="done", runtime_root=tmp_path)
        plan = TaskPlan("plan", "goal", "finish the change", str(tmp_path), status="executing",
                        steps=[PlanStep(step_id="s", index=0, instruction="do it", status="verified")])
        rendered = [planner._resume_brief(plan), json.dumps(await projects.task_detail("done", compact=True), default=str),
                    json.dumps(await projects.task_detail("done"), default=str)]
        assert all(NOTE not in text and "task_feedback" not in text and "defect" not in text for text in rendered)
    run_db(check, monkeypatch, setup=seed)


def test_no_agent_facing_module_reads_task_feedback_or_the_metrics():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "looper"
    readers = {"db/task_feedback.py", "db/metrics.py", "agents/metrics.py", "agents/v1_domains/metrics.py",
               "agents/v1_domains/task_feedback.py", "db/migrations/task_feedback.py"}
    found = {str(path.relative_to(root)) for path in root.rglob("*.py")
             if any(word in path.read_text() for word in ("task_feedback", "db_metrics", "db import metrics"))}
    assert found <= readers, found - readers


def test_task_outcomes_reads_the_window_filters_and_isolates_a_broken_item(monkeypatch, tmp_path):
    async def check(pool):
        now = timedelta(hours=1)
        for i in range(22):
            await task(pool, f"c{i}")
            await outcome(pool, f"c{i}", now, usd=2.0)
        for i in range(3):
            await task(pool, f"f{i}", status="failed")
            await outcome(pool, f"f{i}", now, result="failed", cause="task_budget", kind="task_budget")
        await task(pool, "other", project="q")
        await outcome(pool, "other", now)
        await task(pool, "old")
        await outcome(pool, "old", timedelta(days=10))  # in the comparison window
        await task(pool, "again")
        await outcome(pool, "again", timedelta(days=2), run_no=1, result="stopped", cause="restart")
        await outcome(pool, "again", now, run_no=2)
        await pool.execute("INSERT INTO task_feedback(task_id,verdict) VALUES ('c0','defect')")
        await pool.execute("INSERT INTO task_journal(task_id,step_id,kind,created_at) VALUES ('c1','s','supplement',now()-interval '2 hours')")
        await attempt(pool, "c2", "s1", 1, "first", "accept", now, profile_id="fast")
        await record(pool, "worker-a", "c2", ago=now)  # a Worker run with no attempt
        async with v1_api(tmp_path, pool) as call:
            status, body = await call("metrics.task_outcomes", {"days": 7})
            assert status == 200, body
            result = body["result"]
            assert result["days"] == 7 and result["coverage_since"] is not None and result["missing_attempts"] == 1
            tasks = result["tasks"]
            assert (tasks["success_rate"]["k"], tasks["success_rate"]["n"]) == (24, 27)  # 22 + other + again; "old" ended before the window
            assert tasks["success_rate"]["previous"] == {"insufficient": True, "n": 1}
            assert tasks["budget_failure_rate"]["k"] == 3 and tasks["restarts"]["count"] == 1
            assert tasks["cost_per_completed_task"]["provider_priced"]["mean_usd"] == pytest.approx(46 / 24)
            assert tasks["user_corrections"]["supplement"]["k"] == 1 and tasks["user_corrections"]["feedback"]["k"] == 1
            assert result["reviews"]["defective_deliveries"]["count"] == 1
            only = (await call("metrics.task_outcomes", {"project": "q"}))[1]["result"]
            assert (only["tasks"]["success_rate"]["insufficient"], only["tasks"]["success_rate"]["n"]) == (True, 1) and only["filters"]["project"] == "q"
            fast = (await call("metrics.task_outcomes", {"profile": "fast"}))[1]["result"]
            assert (fast["tasks"]["success_rate"]["insufficient"], fast["tasks"]["success_rate"]["n"]) == (True, 1)
            assert (await call("metrics.task_outcomes", {"days": 0}))[0] == 400
            assert (await call("metrics.task_outcomes", {"difficulty": "easy"}))[0] == 400
            await pool.execute("DROP TABLE task_feedback")
            partial = (await call("metrics.task_outcomes", {}))[1]["result"]
            assert "error" in partial["tasks"]["success_rate"] and "error" in partial["reviews"]["defective_deliveries"]
            assert "error" not in partial["steps"]["first_pass_rate"] and "error" not in partial["contract"]["rates"]
    run_db(check, monkeypatch, setup=seed)


def test_coverage_and_missing_attempts_ignore_runs_before_records_began(monkeypatch, tmp_path):
    async def check(pool):
        await task(pool, "t")
        await outcome(pool, "t", timedelta(hours=1))
        await record(pool, "worker-before", "t", ago=timedelta(days=3))
        first = await attempt(pool, "t", "s1", 1, "first", "accept", timedelta(hours=2))
        await record(pool, "worker-linked", "t", attempt_id=first, ago=timedelta(hours=2))
        async with v1_api(tmp_path, pool) as call:
            result = (await call("metrics.task_outcomes", {}))[1]["result"]
            assert result["missing_attempts"] == 0
            await record(pool, "worker-lost", "t", ago=timedelta(hours=1, minutes=50))
            assert (await call("metrics.task_outcomes", {}))[1]["result"]["missing_attempts"] == 1
    run_db(check, monkeypatch, setup=seed)


def test_contract_rates_come_from_run_outcomes_and_planner_turn_rejections(monkeypatch, tmp_path):
    async def check(pool):
        await task(pool, "t")
        await outcome(pool, "t", timedelta(hours=1))
        for i in range(20):
            await record(pool, f"worker-{i}", "t", metadata={"outcome": "contract" if i < 4 else "completed"})
            await record(pool, f"planner-{i}", "t", metadata={"contract_rejections": {"decision": 1, "plan": 1} if i < 3 else {}})
        await record(pool, "planner-linked", "t", attempt_id=await attempt(pool, "t", "s", 1, "first", "accept",
                                                                         timedelta(hours=2)))
        async with v1_api(tmp_path, pool) as call:
            rates = (await call("metrics.task_outcomes", {}))[1]["result"]["contract"]["rates"]
            assert (rates["worker"]["k"], rates["worker"]["n"]) == (4, 20)
            assert rates["reviewer"] == {"insufficient": True, "n": 0}
            assert (rates["planner"]["k"], rates["planner"]["n"]) == (6, 21)  # every Planner turn, linked or not
    run_db(check, monkeypatch, setup=seed)


def test_step_attempts_adds_up_to_the_task_cost_and_keeps_unattributed_advisor_spend(monkeypatch, tmp_path):
    async def check(pool):
        await task(pool, "t")
        first = await attempt(pool, "t", "s1", 1, "first", "retry", timedelta(hours=3), profile_id="fast",
                              review_outcome="not_passed", review_runs=1, blocking_findings=1,
                              failure_kind="review_rejected")
        retry = await attempt(pool, "t", "s1", 2, "retry", "accept", timedelta(hours=2), review_outcome="passed",
                              review_runs=1)
        await attempt(pool, "t", "s2", 1, "first", None, timedelta(hours=1))
        await record(pool, "planner-t", "t", cost=0.5)
        await record(pool, "worker-1", "t", cost=1.0, attempt_id=first)
        await record(pool, "reviewer-1", "t", cost=0.25, attempt_id=first)
        await record(pool, "planner-t", "t", cost=0.125, attempt_id=first)  # the deciding turn
        advise = await record(pool, "planner-t", "t", cost=0.0625, attempt_id=retry)  # an advise turn, not a decision
        await record(pool, "advisor-x", "t", cost=0.5, parent=advise, kind="advisor")
        checkpoint = await record(pool, "planner-t", "t", cost=0.03125)  # a consult with no proceed after it
        await record(pool, "advisor-y", "t", cost=2.0, parent=checkpoint, kind="advisor")
        await record(pool, "worker-2", "t", cost=3.0, attempt_id=retry, equivalent=True)
        await record(pool, "worker-3", "t", cost=None, attempt_id=retry)
        async with v1_api(tmp_path, pool) as call:
            assert (await call("metrics.step_attempts", {"task_id": "nope"}))[0] == 404
            status, body = await call("metrics.step_attempts", {"task_id": "t"})
            assert status == 200, body
            view = body["result"]
            assert view["summary"]["attempts"] == 3 and view["summary"]["open_attempts"] == 1
            assert view["summary"]["accepted_steps"] == 1 and view["summary"]["first_pass_steps"] == 0
            assert view["summary"]["failure_kinds"] == {"review_rejected": 1}
            s1, s2 = view["steps"]
            assert [a["trigger"] for a in s1["attempts"]] == ["first", "retry"] and s1["accepted"] and not s2["accepted"]
            costs = view["costs"]
            assert costs["first"]["worker"]["provider_priced"]["usd"] == 1.0
            assert costs["first"]["review"]["provider_priced"]["usd"] == 0.25
            assert costs["retry"]["advisor"]["provider_priced"]["usd"] == 0.5
            assert costs["retry"]["worker"]["equivalent"]["usd"] == 3.0
            assert costs["retry"]["worker"]["provider_priced"] == {"usd": None, "runs": 1, "unknown_runs": 1}
            assert costs["unattributed"]["advisor"]["provider_priced"]["usd"] == 2.0
            assert costs["planning"]["provider_priced"]["usd"] == 0.5 + 0.125 + 0.0625 + 0.03125
            paid = llm_records.task_costs
            total = (await paid(["t"]))["t"]
            assert costs["total"]["provider_priced"]["runs"] + costs["total"]["equivalent"]["runs"] == total["runs"]
            assert (round(costs["total"]["provider_priced"]["usd"] + costs["total"]["equivalent"]["usd"], 6)
                    == float(total["cost_usd"]))
            assert s1["attempts"][0]["cost"]["total"]["provider_priced"]["usd"] == 1.25
    run_db(check, monkeypatch, setup=seed)


def test_decisions_come_from_step_attempts_not_from_planner_records_with_an_attempt(monkeypatch, tmp_path):
    async def check(pool):
        await task(pool, "t")
        await outcome(pool, "t", timedelta(hours=1))
        open_attempt = await attempt(pool, "t", "s1", 1, "first", None, timedelta(hours=2))
        for _ in range(3):  # advise turns under an attempt that no decision has settled
            await record(pool, "planner-t", "t", attempt_id=open_attempt, cost=0.01)
        async with v1_api(tmp_path, pool) as call:
            steps = (await call("metrics.task_outcomes", {}))[1]["result"]["steps"]
            assert steps["failure_kinds"] == {"n": 0, "counts": {}, "previous": {"n": 0, "counts": {}}}
            assert steps["rework_rate"] == {"insufficient": True, "n": 0, "previous": {"insufficient": True, "n": 0}}
            view = (await call("metrics.step_attempts", {"task_id": "t"}))[1]["result"]
            assert view["summary"]["open_attempts"] == 1 and view["summary"]["accepted_steps"] == 0
            assert view["costs"]["planning"]["provider_priced"]["runs"] == 3
    run_db(check, monkeypatch, setup=seed)


def test_a_restarted_task_keeps_its_step_history_in_the_read(monkeypatch, tmp_path):
    async def check(pool):
        await task(pool, "t")
        await attempt(pool, "t", "s1", 1, "first", "task_ended", timedelta(hours=3), failure_kind="stopped")
        await pool.execute("UPDATE tasks SET run_no=2 WHERE id='t'")
        await attempt(pool, "t", "s1", 1, "restart", "accept", timedelta(hours=1), task_run_no=2)
        async with v1_api(tmp_path, pool) as call:
            view = (await call("metrics.step_attempts", {"task_id": "t"}))[1]["result"]
            assert [(s["task_run_no"], s["attempts"][0]["trigger"]) for s in view["steps"]] == [(1, "first"), (2, "restart")]
            assert view["run_no"] == 2
    run_db(check, monkeypatch, setup=seed)


def test_compute_runs_on_an_empty_database(monkeypatch):
    async def check(pool):
        result = await metrics.compute(7, metrics.Filters())
        assert result["coverage_since"] is None and result["missing_attempts"] == 0
        assert result["tasks"]["success_rate"] == {"insufficient": True, "n": 0,
                                                    "previous": {"insufficient": True, "n": 0}}
        assert all("error" not in item for group in ("tasks", "steps", "reviews", "contract")
                   for item in result[group].values())
    run_db(check, monkeypatch, setup=seed)


def test_contract_counts_only_runs_that_ended_inside_the_window(monkeypatch):
    """One task, two runs: 20 completed Worker runs on the run that ended 10 days ago, and 1 contract Worker run on
    the run that ended an hour ago. Each window counts its own run (0/20 and 1/1), including when both runs used
    the same Profile. A Worker run from before the first run starts is in neither."""
    async def check(pool):
        from looper.db import metrics as db_metrics

        await task(pool, "t")
        await outcome(pool, "t", timedelta(days=10), run_no=1, minutes=120)
        await attempt(pool, "t", "s1", 1, "first", "accept", timedelta(days=10), profile_id="fast", task_run_no=1)
        await record(pool, "worker-before", "t", metadata={"outcome": "contract"}, ago=timedelta(days=11))
        for i in range(20):
            await record(pool, f"worker-old-{i}", "t", metadata={"outcome": "completed"},
                         ago=timedelta(days=10) - timedelta(minutes=30))
        await outcome(pool, "t", timedelta(hours=1), run_no=2, minutes=120)
        await attempt(pool, "t", "s2", 1, "first", "accept", timedelta(hours=1), profile_id="fast", task_run_no=2)
        await record(pool, "worker-new", "t", metadata={"outcome": "contract"}, ago=timedelta(minutes=30))
        now = datetime.now(timezone.utc)
        split = now - timedelta(days=7)

        async def counts(since, until, profile):
            row = await db_metrics.contract_counts(since, until, None, profile, None, None, pool=pool)
            return row["worker_contract"], row["worker_runs"]

        assert await counts(split - timedelta(days=28), split, None) == (0, 20)
        assert await counts(split, now, None) == (1, 1)
        assert await counts(split - timedelta(days=28), split, "fast") == (0, 20)
        assert await counts(split, now, "fast") == (1, 1)
        rates = (await metrics.compute(7, metrics.Filters(), pool=pool))["contract"]["rates"]
        assert (rates["previous"]["worker"]["k"], rates["previous"]["worker"]["n"]) == (0, 20)
        assert rates["worker"] == {"insufficient": True, "n": 1}
    run_db(check, monkeypatch, setup=seed)


class _IndexedReads:
    """EXPLAIN each read before it runs, and refuse a sequential scan of the two large tables."""

    def __init__(self, pool):
        self.pool = pool

    async def _check(self, sql, args):
        if not sql.lstrip()[:6].upper().startswith(("SELECT", "WITH")):
            return
        plan = "\n".join(row[0] for row in await self.pool.fetch("EXPLAIN " + sql, *args))
        assert "Seq Scan on step_attempts" not in plan, plan
        assert "Seq Scan on llm_records" not in plan, plan

    async def execute(self, sql, *args):
        await self._check(sql, args)
        return await self.pool.execute(sql, *args)

    async def fetch(self, sql, *args):
        await self._check(sql, args)
        return await self.pool.fetch(sql, *args)

    async def fetchrow(self, sql, *args):
        await self._check(sql, args)
        return await self.pool.fetchrow(sql, *args)

    async def fetchval(self, sql, *args):
        await self._check(sql, args)
        return await self.pool.fetchval(sql, *args)


def test_comparison_window_and_profile_reads_do_not_scan_the_large_tables(monkeypatch):
    """100k attempts and 200k Worker runs sit on a task whose run ended in the comparison window, but the rows
    themselves are far older than that run. The comparison read and a Profile filter must not sequentially scan
    ``step_attempts`` or ``llm_records`` to ignore them."""
    async def check(pool):
        from looper.db import metrics as db_metrics

        await task(pool, "hist")
        await outcome(pool, "hist", timedelta(days=10), minutes=120)
        await attempt(pool, "hist", "s", 1, "first", "accept", timedelta(days=10), profile_id="fast")
        await pool.execute(
            """
            INSERT INTO step_attempts(task_id, task_run_no, step_key, attempt, trigger, profile_id, decision,
                                      started_at, decided_at)
            SELECT 'hist', 1, 'old' || i, 1, 'first', 'old', 'accept', ts, ts
            FROM generate_series(1, 100000) AS i, (SELECT now() - interval '400 days' AS ts) AS clock
            """)
        await pool.execute(
            """
            INSERT INTO llm_records(agent_id, task_id, run_kind, metadata, created_at)
            SELECT 'worker-bulk', 'hist', 'worker', '{"outcome":"completed"}'::jsonb, now() - interval '400 days'
            FROM generate_series(1, 200000)
            """)
        await pool.execute("ANALYZE step_attempts, llm_records, task_outcomes, tasks")
        now = datetime.now(timezone.utc)
        split = now - timedelta(days=7)
        since, until = split - timedelta(days=28), split
        watched = _IndexedReads(pool)
        await db_metrics.attempt_rows(since, until, None, pool=watched)
        await db_metrics.contract_counts(since, until, None, None, None, since, pool=watched)
        await db_metrics.task_rows(since, until, None, "fast", None, pool=watched)
        await db_metrics.contract_counts(since, until, None, "fast", None, since, pool=watched)
    run_db(check, monkeypatch, setup=seed, timeout=180)
