"""Workflow definitions: references, typed limits and fixed Profile compatibility."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from looper.agents import profile_registry
from looper.agents.workflows import Workflow, WorkflowError, check_profiles


def definition(**extra):
    return {"key": "reviewed", "name": "Reviewed change", "roles": [
        {"key": "dev", "kind": "work", "role_prompt": "Implement the change."},
        {"key": "review_a", "kind": "review", "role_prompt": "Review correctness."},
        {"key": "verify_staging", "kind": "work", "role_prompt": "Verify the change."},
    ], "work_role": "dev", **extra}


def test_snapshot_round_trip_preserves_the_definition_and_passed_panel_semantics():
    workflow = Workflow.from_dict(definition(
        review_panel={"members": ["review_a", "builtin"], "accept_when": {"all_of": ["review_a"]}},
        distinct_reviewer_runtime=True,
        verification={"role": "verify_staging", "max_rounds": 2}, max_step_retries=0, budget_usd=2.5))
    snapshot = json.loads(json.dumps(workflow.snapshot()))
    assert Workflow.from_dict(snapshot) == workflow
    assert snapshot["review_panel"]["accept_when"] == {"all_of": [{"step": "review_a", "on": "passed"}]}
    assert Workflow.from_row({"key": workflow.key, "name": workflow.name, "description": "",
                              "definition": json.dumps(snapshot)}) == workflow


@pytest.mark.parametrize("extra,match", [
    ({"key": "none"}, "key"),
    ({"name": ""}, "name"),
    ({"description": "x" * 301}, "description"),
    ({"planner_instructions": "x" * 8001}, "planner_instructions"),
    ({"review_all": "yes"}, "review_all"),
    ({"max_step_retries": True}, "max_step_retries"),
    ({"max_step_retries": 21}, "max_step_retries"),
    ({"budget_usd": float("nan")}, "budget_usd"),
    ({"budget_usd": 0.00001}, "4 decimals"),
    ({"budget_usd": 100_000.0001}, "100000"),
    ({"budget_usd": 0}, "more than 0"),
    ({"budget_usd": True}, "budget_usd"),
    ({"profiles": {"manager": "p"}}, "profiles"),
    ({"roles": [{}] * 9}, "roles"),
    ({"roles": [{"key": "dev", "kind": "work", "role_prompt": ""}]}, "role_prompt"),
    ({"work_role": "review_a"}, "work role"),
    ({"verification": {"role": "review_a", "max_rounds": 1}}, "work role"),
    ({"verification": {"role": "verify_staging", "max_rounds": 0}}, "max_rounds"),
])
def test_definition_refuses_invalid_fields(extra, match):
    with pytest.raises(WorkflowError, match=match):
        Workflow.from_dict(definition(**extra))


@pytest.mark.parametrize("budget", [None, 0.0001, 1.2345, 100_000])
def test_budget_accepts_runtime_precision_and_range(budget):
    assert Workflow.from_dict(definition(budget_usd=budget)).budget_usd == budget


@pytest.mark.parametrize("panel,match", [
    ({"members": ["review_a"], "accept_when": {"all_of": []}}, "non-empty"),
    ({"members": ["review_a"], "accept_when": "review_b"}, "only panel members"),
    ({"members": ["review_a"], "accept_when": {"step": "review_a", "on": "completed"}}, "passed"),
    ({"members": ["dev"], "accept_when": "dev"}, "review role"),
    ({"members": ["review_a", "review_a"], "accept_when": "review_a"}, "twice"),
])
def test_panel_requires_review_members_and_nonempty_acceptance(panel, match):
    with pytest.raises(WorkflowError, match=match):
        Workflow.from_dict(definition(review_panel=panel))


@pytest.mark.parametrize("distinct", [False, None])
def test_panel_requires_distinct_reviewer_runtime(distinct):
    raw = definition(review_panel={"members": ["review_a"], "accept_when": "review_a"})
    if distinct is not None:
        raw["distinct_reviewer_runtime"] = distinct
    with pytest.raises(WorkflowError, match="review_panel requires distinct_reviewer_runtime to be true"):
        Workflow.from_dict(raw)


def test_profiles_are_checked_for_the_agent_that_runs_them(monkeypatch):
    async def selection(profile_id, roles, **kwargs):
        expected = {"p": "planner", "w": "worker", "r": "reviewer"}
        if roles != [expected[profile_id]]:
            raise AssertionError((profile_id, roles))
        return SimpleNamespace(runtime=SimpleNamespace(id=profile_id))

    monkeypatch.setattr(profile_registry, "check_selection", selection)
    workflow = Workflow.from_dict(definition(profiles={"planner": "p", "worker": "w", "reviewer": "r"}))
    asyncio.run(check_profiles(workflow))


def test_incompatible_profile_errors_are_workflow_errors(monkeypatch):
    async def selection(*args, **kwargs):
        raise profile_registry.RegistryError("cannot run reviewer")

    monkeypatch.setattr(profile_registry, "check_selection", selection)
    with pytest.raises(WorkflowError, match="cannot run reviewer"):
        asyncio.run(check_profiles(Workflow.from_dict(definition(profiles={"reviewer": "p"}))))


@pytest.mark.parametrize("worker,reviewer,distinct,rejected", [
    ("same", "same", True, True),
    ("same", "other", True, False),
    (None, "same", True, False),
    ("same", "same", False, False),
])
def test_distinct_runtime_checks_only_both_fixed_profiles(monkeypatch, worker, reviewer, distinct, rejected):
    async def selection(profile_id, roles, **kwargs):
        return SimpleNamespace(runtime=SimpleNamespace(id=profile_id))

    monkeypatch.setattr(profile_registry, "check_selection", selection)
    profiles = {key: value for key, value in (("worker", worker), ("reviewer", reviewer)) if value}
    workflow = Workflow.from_dict(definition(profiles=profiles, distinct_reviewer_runtime=distinct,
        **({"review_panel": {"members": ["review_a"], "accept_when": "review_a"}} if distinct else {})))
    if rejected:
        with pytest.raises(WorkflowError, match="different runtime"):
            asyncio.run(check_profiles(workflow))
    else:
        asyncio.run(check_profiles(workflow))


def test_fixed_task_profiles_win_over_role_profiles_for_distinct_runtime(monkeypatch):
    async def selection(profile_id, roles, **kwargs):
        return SimpleNamespace(runtime=SimpleNamespace(id=profile_id))

    monkeypatch.setattr(profile_registry, "check_selection", selection)
    raw = definition(profiles={"worker": "same", "reviewer": "same"}, distinct_reviewer_runtime=True,
                     review_panel={"members": ["review_a"], "accept_when": "review_a"})
    raw["roles"][0]["profile_id"] = "work-runtime"
    with pytest.raises(WorkflowError, match="different runtime"):
        asyncio.run(check_profiles(Workflow.from_dict(raw)))


def test_workflow_upgrade_is_named_idempotent_and_widens_role_provenance(monkeypatch):
    from tests.db_support import run_db
    from looper.db import connection

    async def test(pool):
        await pool.execute("""ALTER TABLE projects DROP COLUMN default_workflow;
            ALTER TABLE tasks DROP COLUMN workflow;
            DROP TABLE workflows;
            ALTER TABLE task_roles DROP CONSTRAINT task_roles_via_check;
            ALTER TABLE task_roles ADD CONSTRAINT task_roles_via_check CHECK (via IN ('planner', 'user', 'template'));
            DELETE FROM schema_migrations WHERE name = 'workflows'""")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name = 'workflows'") == 1
        constraint = await pool.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'task_roles_via_check' AND conrelid = 'task_roles'::regclass")
        assert "'workflow'" in constraint and "'template'" in constraint
        assert await pool.fetchval("SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND column_name IN ('default_workflow', 'workflow')") == 2

    run_db(test, monkeypatch, timeout=30)
