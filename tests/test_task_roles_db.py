"""The roles a task's Planner defines, on a real PostgreSQL: its tools define them only where its project lets it,
each change is a new version with a journal entry, a definition Looper refuses changes nothing, and a run of a role
gets its profile under the user's choice for the task and over the Planner's own choices; planning the task anew drops
its roles, going on from its plan keeps them.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.test_ops_tasks_db import _error, _project, _task
from tests.test_planner_tools_db import _allow, _haiku, _journal, _ops, _Planner, _steps
from tests.test_task_agents_db import _call, _profiles
from tests.test_usage_api_db import DSN, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

TESTER = {"id": "tester", "kind": "work", "role_prompt": "You write the tests of the step's code.",
          "reason": "the tests need their own author"}


async def _roles_on(pool: Any, project: dict[str, Any], **settings: Any) -> None:
    """Let ``project``'s Planners define roles (``planner_roles`` on), with its other execution ``settings``."""
    status, answer = await _call("PUT", f"/api/projects/{project['id']}", {"planner_roles": "on", **settings})
    assert status == 200, answer


async def _versions(pool: Any, task_id: str) -> list[tuple[str, int, str | None, str | None]]:
    return [(r["role_key"], r["version"], r["profile_id"], r["harness"]) for r in await pool.fetch(
        "SELECT role_key, version, profile_id, harness FROM task_roles WHERE task_id = $1 ORDER BY role_key, version",
        task_id)]


def test_the_role_tools_are_listed_and_work_only_where_the_project_lets_its_planner_define_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        ops = _ops()
        planner = _Planner(ops, "task-a")
        header = f"Bearer {planner.token}"
        base = ["looper_task_profiles", "looper_task_profile_set", "looper_task_concurrency"]

        # Off by default: the tools are not listed, a call is refused and writes nothing, and the guide is as before.
        assert [t["name"] for t in await ops.listing(header)] == base
        answer = await planner("looper_task_role_set", TESTER)
        assert _error(answer) == "forbidden" and "planner_roles is off" in answer["error"]["message"]
        assert _error(await planner("looper_task_roles", {})) == "forbidden"
        assert await _versions(pool, "task-a") == [] and await _journal(pool, "task-a", "role_defined") == []
        assert ops.guide_for(roles=False) == ops.guide

        await _roles_on(pool, project)
        assert [t["name"] for t in await ops.listing(header)] == [*base, "looper_task_roles", "looper_task_role_set"]
        assert "looper_task_role_set" in ops.guide_for(roles=True)
        answer = await planner("looper_task_role_set", TESTER)
        assert answer["status"] == "ok" and answer["result"]["version"] == 1, answer
        assert await _versions(pool, "task-a") == [("tester", 1, None, None)]

        # Switched off again: its roles stay, but none is defined any more.
        status, _ = await _call("PUT", f"/api/projects/{project['id']}", {"planner_roles": None})
        assert status == 200
        assert _error(await planner("looper_task_role_set", {**TESTER, "role_prompt": "other"})) == "forbidden"
        assert await _versions(pool, "task-a") == [("tester", 1, None, None)]
        # A token the endpoint does not know lists no role tool either.
        assert "looper_task_roles" not in {t["name"] for t in await ops.listing("Bearer planner.nope")}

    _run(test, monkeypatch)


def test_each_change_is_a_new_version_with_a_journal_entry_and_a_refused_one_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _steps(pool, "task-a", s1="pending")
        await pool.execute("UPDATE task_steps SET role_key = 'tester' WHERE id = 'step-s1'")
        await _roles_on(pool, project, role_text_max_chars=500)
        planner = _Planner(_ops(), "task-a")

        first = await planner("looper_task_role_set", TESTER)
        assert first["result"] == {**first["result"], "version": 1, "changed": True, "steps": ["s1"]}
        same = await planner("looper_task_role_set", TESTER)
        assert same["result"]["changed"] is False and same["result"]["version"] == 1
        second = await planner("looper_task_role_set", {**TESTER, "harness": "\n## How you work\nTest first.\n"})
        assert second["result"]["version"] == 2
        assert await _versions(pool, "task-a") == [("tester", 1, None, None),
                                                   ("tester", 2, None, "## How you work\nTest first.")]
        entries = await _journal(pool, "task-a", "role_defined")
        assert [step for step, _ in entries] == ["", ""]
        assert entries[0][1].startswith("Role tester (work) v1: profile as the built-in Worker; harness the working "
                                        "style that applies; role prompt 39 characters. Reason: the tests need")
        assert "v2" in entries[1][1] and "harness its own" in entries[1][1]

        refused = [
            ({**TESTER, "id": "Worker"}, "invalid", "names a built-in role"),
            ({**TESTER, "id": "has space"}, "invalid", "short label"),
            ({**TESTER, "kind": "review"}, "invalid", "tester is a work role; a role keeps its kind"),
            ({**TESTER, "role_prompt": "   "}, "invalid", "role_prompt"),
            ({**TESTER, "role_prompt": "x" * 501}, "invalid", "this project allows at most 500"),
            ({**TESTER, "harness": "x" * 501}, "invalid", "harness has 501 characters"),
            ({**TESTER, "profile_id": "claude-code/sonnet"}, "forbidden", "its list is empty"),
            ({**TESTER, "kind": "plan"}, "invalid", ""),
        ]
        for arguments, code, message in refused:
            answer = await planner("looper_task_role_set", arguments)
            assert _error(answer) == code and message in answer["error"]["message"], (arguments, answer)
        assert len(await _versions(pool, "task-a")) == 2 and len(await _journal(pool, "task-a", "role_defined")) == 2

        # Outside a turn Looper sent it, the Planner reads its roles but defines none.
        planner.ops.end_turn(planner.token)
        assert _error(await planner("looper_task_role_set", {**TESTER, "id": "doc"})) == "needs_user_turn"
        read = await planner("looper_task_roles", {})
        assert [(r["id"], r["kind"], r["version"], r["steps"]) for r in read["result"]["roles"]] == [
            ("tester", "work", 2, ["s1"])]
        assert read["result"]["role_text_max_chars"] == 500 and read["result"]["allowed_profiles"] == []

    _run(test, monkeypatch)


def test_a_roles_profile_comes_from_the_projects_list_and_yields_to_the_users_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def test(pool: Any) -> None:
        await _profiles(pool)
        await _haiku(pool)
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _roles_on(pool, project)
        await _allow(project, "agent-sonnet", "agent-one-off")
        planner = _Planner(_ops(), "task-a")

        answer = await planner("looper_task_role_set", {**TESTER, "profile_id": "claude-code/haiku"})
        assert _error(answer) == "forbidden" and "not on the project's list" in answer["error"]["message"]
        answer = await planner("looper_task_role_set", {**TESTER, "profile_id": "claude-code/sonnet"})
        assert answer["status"] == "ok"
        assert await _versions(pool, "task-a") == [("tester", 1, "agent-sonnet", None)]
        read = (await planner("looper_task_roles", {}))["result"]
        assert read["roles"][0]["profile"] == "claude-code/sonnet"
        assert {p["name"]: p["kinds"] for p in read["allowed_profiles"]} == {
            "claude-code/sonnet": ["work", "review"], "one-off/opus": ["work", "review"]}

        # The user chose the task's Workers' profile: a work role takes no profile of its own any more.
        status, _ = await _call("PUT", "/api/tasks/task-a/agents", {"profiles": {"worker": "claude-code/haiku"}})
        assert status == 200
        answer = await planner("looper_task_role_set", {**TESTER, "id": "doc", "profile_id": "claude-code/sonnet"})
        assert _error(answer) == "forbidden" and "their choice stands" in answer["error"]["message"]
        assert (await planner("looper_task_role_set", {**TESTER, "id": "doc"}))["status"] == "ok"

    _run(test, monkeypatch)


def test_a_run_of_a_role_gets_its_profile_under_the_users_choice_and_over_the_planners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents.agent_settings import load_agent_settings

    async def runs_on(task_id: str, project: dict[str, Any], role: str, task_role: tuple[str, int] | None,
                      step: str = "s1") -> tuple[str, str]:
        settings = await load_agent_settings(role, project["id"], task_id, step_id=f"step-{step}",
                                             task_role=task_role)
        return settings.profile.name, settings.profile_source.value

    async def test(pool: Any) -> None:
        await _profiles(pool)
        await _haiku(pool)
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _steps(pool, "task-a", s1="pending")
        await _roles_on(pool, project)
        await _allow(project, "agent-sonnet", "agent-haiku", "agent-claude-code-opus")
        planner = _Planner(_ops(), "task-a")
        await planner("looper_task_role_set", {**TESTER, "profile_id": "claude-code/sonnet"})
        await planner("looper_task_role_set", {"id": "security", "kind": "review", "role_prompt": "Check security."})

        # Without a profile of its own a role runs what its kind's built-in role would; the Planner's choice for
        # the step wins over its choice for the rest of the task, the role's profile over both.
        await planner("looper_task_profile_set", {"role": "worker", "profile_id": "claude-code/haiku"})
        await planner("looper_task_profile_set", {"role": "reviewer", "profile_id": "claude-code/haiku",
                                                  "step": "s1"})
        assert await runs_on("task-a", project, "worker", None) == ("claude-code/haiku", "planner_task")
        assert await runs_on("task-a", project, "worker", ("tester", 1)) == ("claude-code/sonnet", "task_role")
        await planner("looper_task_profile_set", {"role": "worker", "profile_id": "claude-code/opus",
                                                  "step": "s1"})
        assert await runs_on("task-a", project, "worker", None) == ("claude-code/opus", "planner_step")
        assert await runs_on("task-a", project, "worker", ("tester", 1)) == ("claude-code/sonnet", "task_role")
        assert await runs_on("task-a", project, "reviewer", ("security", 1)) == ("claude-code/haiku", "planner_step")
        assert await runs_on("task-a", project, "reviewer", None, step="s9") == ("claude-code/opus", "builtin")

        # A profile the project no longer allows counts as none: the run falls back to what its kind would run.
        status, _ = await _call("PUT", f"/api/projects/{project['id']}/allowed-profiles",
                                {"profile_ids": ["agent-haiku", "agent-claude-code-opus"]})
        assert status == 200
        assert await runs_on("task-a", project, "worker", ("tester", 1)) == ("claude-code/opus", "planner_step")
        read = (await planner("looper_task_roles", {}))["result"]
        assert read["roles"][0]["profile"] is None and read["roles"][0]["profile_not_allowed"] == "claude-code/sonnet"
        await _allow(project, "agent-sonnet", "agent-haiku", "agent-claude-code-opus")

        # The user's choice for the task wins over every other.
        status, _ = await _call("PUT", "/api/tasks/task-a/agents", {"profiles": {"worker": "claude-code/opus"}})
        assert status == 200
        assert await runs_on("task-a", project, "worker", ("tester", 1)) == ("claude-code/opus", "task")

    _run(test, monkeypatch)


def test_planning_a_task_anew_drops_its_roles_and_going_on_from_its_plan_keeps_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.db import tasks

    async def test(pool: Any) -> None:
        project = await _project(pool)
        for task_id in ("task-a", "task-b"):
            await _task(pool, task_id, "running", project)
        await _roles_on(pool, project)
        for task_id in ("task-a", "task-b"):
            assert (await _Planner(_ops(), task_id)("looper_task_role_set", TESTER))["status"] == "ok"
            await pool.execute("UPDATE tasks SET status = 'failed' WHERE id = $1", task_id)

        await tasks.reset_task_for_restart("task-a", keep_steps=True, pool=pool)
        await tasks.reset_task_for_restart("task-b", keep_steps=False, pool=pool)
        assert await _versions(pool, "task-a") == [("tester", 1, None, None)]
        assert await _versions(pool, "task-b") == []

    _run(test, monkeypatch)


def test_the_task_page_lists_each_roles_versions_steps_and_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(pool: Any, agent_id: str, step: str, outcome: str, cost: float, role: str | None = None) -> None:
        layers = {"task_role": {"id": role, "kind": "work", "version": 1}} if role else {}
        await pool.execute(
            "INSERT INTO llm_records (agent_id, task_id, status, cost_usd, metadata) VALUES ($1, 'task-a', "
            "'completed', $2, $3::jsonb)", agent_id, cost,
            json.dumps({"step_id": f"step-{step}", "outcome": outcome, "prompt_layers": layers}))

    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await _steps(pool, "task-a", s1="verified", s2="failed", s3="verified")
        await pool.execute("UPDATE task_steps SET role_key = 'tester', review = true, retries = 1 "
                           "WHERE id IN ('step-s1', 'step-s2')")
        await pool.execute("UPDATE task_steps SET review = true, review_roles = '[{\"role\": \"security\", "
                           "\"required\": true, \"version\": 1}]' WHERE id = 'step-s1'")
        await _roles_on(pool, project)
        planner = _Planner(_ops(), "task-a")
        await planner("looper_task_role_set", TESTER)
        await planner("looper_task_role_set", {**TESTER, "role_prompt": "You write the tests first."})
        await planner("looper_task_role_set", {"id": "security", "kind": "review", "role_prompt": "Check security."})
        await run(pool, "worker-1", "s1", "contract", 0.1, "tester")
        await run(pool, "worker-2", "s1", "completed", 0.2, "tester")
        await run(pool, "worker-3", "s2", "reported_failed", 0.3, "tester")
        await run(pool, "worker-4", "s3", "completed", 0.4)
        await run(pool, "reviewer-1", "s1", "passed", 0.05)
        await run(pool, "reviewer-2", "s2", "not_passed", 0.05)
        await run(pool, "reviewer-3", "s1", "not_passed", 0.01, "security")
        await run(pool, "reviewer-4", "s1", "passed", 0.01, "security")

        status, detail = await _call("GET", "/api/tasks/task-a")
        assert status == 200
        roles = {role["id"]: role for role in detail["roles"]}
        assert [role["id"] for role in detail["roles"]] == ["tester", "security"]
        tester, security = roles["tester"], roles["security"]
        assert (tester["version"], [v["version"] for v in tester["versions"]], tester["steps"]) == (2, [1, 2],
                                                                                                    ["s1", "s2"])
        assert tester["role_prompt"] == "You write the tests first." and tester["profile"] is None
        assert tester["stats"] == {
            "runs": 3, "running": 0, "cost_usd": 0.6, "contract_violations": 1, "contract_violation_rate": 0.3333,
            "steps": 2, "accepted": 1, "failed": 1, "retries": 2, "reviews": {"passed": 1, "not_passed": 1},
            "review_pass_rate": 0.5}
        assert (security["kind"], security["steps"], security["stats"]["runs"], security["stats"]["reviews"],
                security["stats"]["review_pass_rate"]) == ("review", ["s1"], 2, {"passed": 1, "not_passed": 1}, 0.5)
        builtin = detail["builtin_role_stats"]
        assert (builtin["worker"]["runs"], builtin["worker"]["steps"], builtin["worker"]["reviews"]) == (
            1, 1, {"passed": 0, "not_passed": 0})
        assert (builtin["reviewer"]["runs"], builtin["reviewer"]["reviews"]) == (2, {"passed": 1, "not_passed": 1})
        assert [e["kind"] for e in detail["journal"]].count("role_defined") == 3

        # The Manager's view of the task names its roles and each step's, and of a task without any neither.
        from looper.agents.ops.tasks import _task_view

        view = await _task_view("task-a")
        assert view["roles"] == [{"id": "tester", "kind": "work", "version": 2},
                                 {"id": "security", "kind": "review", "version": 1}]
        assert [{k: s.get(k) for k in ("step", "role", "review_roles")} for s in view["steps"]] == [
            {"step": "s1", "role": "tester", "review_roles": ["security"]},
            {"step": "s2", "role": "tester", "review_roles": None}, {"step": "s3", "role": None, "review_roles": None}]
        assert "role" not in view["steps"][2]
        await _task(pool, "task-b", "running", project)
        assert "roles" not in await _task_view("task-b")

    _run(test, monkeypatch)


def test_a_steps_roles_and_its_review_roles_are_saved_and_read_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import plans

    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        review_roles = [{"role": "security", "required": True, "version": 3},
                        {"role": "style", "required": False, "version": 1}]
        step = {"id": "step-s1", "index": 0, "key": "s1", "instruction": "do it", "review": True,
                "role_key": "tester", "role_version": 2, "review_roles": review_roles, "review_stage": "role",
                "review_position": 1}
        plain = {"id": "step-s2", "index": 1, "key": "s2", "instruction": "do it"}
        await plans.save("task-a", plan={"plan_id": "p", "status": "executing"}, steps=[step, plain], pool=pool)
        rows = {s["step_key"]: s for s in (await plans.load("task-a", pool=pool))["steps"]}
        assert {k: rows["s1"][k] for k in ("role_key", "role_version", "review_roles", "review_stage",
                                            "review_position")} == {
            "role_key": "tester", "role_version": 2, "review_roles": review_roles, "review_stage": "role",
            "review_position": 1}
        assert (rows["s2"]["review_roles"], rows["s2"]["review_position"]) == ([], None)
        # Review roles need a reviewed step, in the table too.
        with pytest.raises(Exception, match="task_steps_review_roles_reviewed"):
            await plans.save("task-a", steps=[{**step, "review": False}], pool=pool)

    _run(test, monkeypatch)


def test_a_v2_7_0_steps_one_review_role_becomes_a_list_of_one_and_its_columns_go(monkeypatch: pytest.MonkeyPatch,
                                                                                 ) -> None:
    """A step of a v2.7.0 database had one review role (``review_role_key``, ``review_role_required``,
    ``review_role_version``) and its result one verdict (``role_review``): the schema makes each a list of one, keeps
    which review a step in review waits for, and drops the columns and their constraint."""
    from looper.db import steps
    from looper.db.connection import ensure_schema

    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _task(pool, "task-a", "running", project)
        await pool.execute("ALTER TABLE task_steps DROP CONSTRAINT task_steps_review_roles_reviewed, "
                           "DROP COLUMN review_roles, DROP COLUMN review_position, "
                           "ADD COLUMN review_role_key TEXT, "
                           "ADD COLUMN review_role_required BOOLEAN NOT NULL DEFAULT true, "
                           "ADD COLUMN review_role_version INTEGER, "
                           "ADD CONSTRAINT task_steps_review_role_reviewed CHECK (review_role_key IS NULL OR review)")
        verdict = {"role": "style", "version": 1, "passed": False, "findings": ["no docstring"], "summary": "no",
                   "learnings": []}
        for step_id, index, review_role, required, stage, result in (
                ("step-a", 0, "style", False, "role", {"status": "completed", "role_review": verdict}),
                ("step-b", 1, "security", True, "builtin", {"status": "completed"}),
                ("step-c", 2, None, True, None, None)):
            await pool.execute(
                "INSERT INTO task_steps (id, task_id, step_index, step_key, instruction, review, status, result, "
                "review_role_key, review_role_required, review_role_version, review_stage) VALUES ($1, 'task-a', $2, "
                "$1, 'do it', $3, 'completed', $4::jsonb, $5, $6, $7, $8)", step_id, index, review_role is not None,
                None if result is None else json.dumps(result), review_role, required,
                1 if review_role else None, stage)

        await ensure_schema(pool)

        rows = {s["id"]: s for s in await steps.list_steps("task-a", pool=pool)}
        assert (rows["step-a"]["review_roles"], rows["step-a"]["review_position"]) == (
            [{"role": "style", "required": False, "version": 1}], 0)
        assert rows["step-a"]["result"] == {"status": "completed", "role_reviews": [{**verdict, "required": False}]}
        assert (rows["step-b"]["review_roles"], rows["step-b"]["review_position"]) == (
            [{"role": "security", "required": True, "version": 1}], None)
        assert rows["step-b"]["result"] == {"status": "completed"}
        assert (rows["step-c"]["review_roles"], rows["step-c"]["result"]) == ([], None)
        columns = {r["column_name"] for r in await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'task_steps'")}
        assert not columns & {"review_role_key", "review_role_required", "review_role_version"}
        constraints = {r["conname"] for r in await pool.fetch(
            "SELECT conname FROM pg_constraint WHERE conrelid = to_regclass('task_steps')")}
        assert "task_steps_review_roles_reviewed" in constraints
        assert "task_steps_review_role_reviewed" not in constraints
        await ensure_schema(pool)  # and again, a no-op

    _run(test, monkeypatch)
