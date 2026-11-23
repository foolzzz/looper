"""Admission reads persisted attempt identity when recovery dispatches lack flags."""

import pytest

from looper.agents import admission, attempts
from looper.agents.agent_settings import AgentType
from looper.agents.lanes import Lane
from looper.db import llm_records
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def seed(pool):
    await pool.execute("INSERT INTO projects(id,name) VALUES ('p','Synthetic project')")
    await pool.execute(
        "INSERT INTO tasks(id,requirement,project_id) VALUES ('t','Synthetic task','p')"
    )
    await pool.execute(
        "INSERT INTO task_steps(id,task_id,step_index,instruction,step_key) "
        "VALUES ('s','t',0,'Synthetic step','s1')"
    )


def test_worker_first_dispatch_and_same_attempt_recovery(monkeypatch):
    async def test(pool):
        monkeypatch.setattr(admission._controller, "active", True)
        attempt = await attempts.open_attempt("t", "s", "s1", 1, attempts.Trigger.FIRST)
        payload = {"attempt_id": attempt, "task_id": "s"}
        assert await admission.worker_lane("t", payload) is Lane.USER_TASK
        record = await llm_records.start_record(agent_id="worker-first", task_id="t")
        await attempts.worker_started(attempt, record, None)
        assert await admission.worker_lane("t", payload) is Lane.RECOVERY

    run_db(test, monkeypatch, setup=seed)


def test_restart_trigger_survives_dispatch_without_trigger_field(monkeypatch):
    async def test(pool):
        monkeypatch.setattr(admission._controller, "active", True)
        attempt = await attempts.open_attempt(
            "t", "s", "s1", 1, attempts.Trigger.RESTART
        )
        assert (
            await admission.worker_lane("t", {"attempt_id": attempt}) is Lane.RECOVERY
        )

    run_db(test, monkeypatch, setup=seed)


def test_review_recovery_matches_the_review_seat(monkeypatch):
    async def test(pool):
        monkeypatch.setattr(admission._controller, "active", True)
        attempt = await attempts.open_attempt("t", "s", "s1", 1, attempts.Trigger.FIRST)
        worker = await llm_records.start_record(agent_id="worker-first", task_id="t")
        await attempts.worker_started(attempt, worker, None)
        payload = {"attempt_id": attempt, "task_id": "review-seat-one"}
        assert (
            await admission.worker_lane("t", payload, role=AgentType.REVIEWER)
            is Lane.USER_TASK
        )
        record = await llm_records.start_record(
            agent_id="reviewer-first",
            task_id="t",
            run_kind="reviewer",
            metadata={"step_id": "review-seat-one"},
        )
        await attempts.link_record(attempt, record)
        assert (
            await admission.worker_lane("t", payload, role=AgentType.REVIEWER)
            is Lane.RECOVERY
        )
        payload["task_id"] = "review-seat-two"
        assert (
            await admission.worker_lane("t", payload, role=AgentType.REVIEWER)
            is Lane.USER_TASK
        )

    run_db(test, monkeypatch, setup=seed)
