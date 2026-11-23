"""Attempt storage against a disposable PostgreSQL database."""

import pytest

from looper.agents import attempts
from looper.agents.attempts import Decision, FailureKind, ReviewResult, RunFailure, RunResult, Trigger
from looper.agents.tiers.worker import RunOutcome
from looper.db import llm_records, step_attempts
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def seed(pool):
    await pool.execute("INSERT INTO projects(id,name) VALUES ('p','Synthetic project')")
    await pool.execute("INSERT INTO tasks(id,requirement,project_id) VALUES ('t','Synthetic task','p')")
    await pool.execute("INSERT INTO task_steps(id,task_id,step_index,instruction,step_key) "
                       "VALUES ('s','t',0,'Synthetic step','s1')")


def test_attempts_number_retries_restarts_and_resume_without_replacing_history(monkeypatch):
    async def test(pool):
        first = await attempts.open_attempt('t', 's', 's1', 1, Trigger.FIRST)
        assert first is not None
        assert await attempts.current_attempt('t', 's1', 1) == first
        worker = await llm_records.start_record(agent_id='worker-test', task_id='t')
        fallback = await llm_records.start_record(agent_id='worker-fallback', task_id='t')
        for record, profile in [(worker, 'one'), (fallback, 'two')]:
            await attempts.worker_started(first, record, profile)
        await attempts.worker_finished(first, RunOutcome.RUN_FAILED, RunFailure.TIMEOUT)
        review = await llm_records.start_record(agent_id='reviewer-test', task_id='t')
        await attempts.link_record(first, review)
        await attempts.review_finished(first, True, 0)
        await attempts.review_finished(first, False, 2)
        await attempts.review_finished(first, True, 0)
        planner = await llm_records.start_record(agent_id='planner-t', task_id='t')
        await attempts.decide_attempt(first, Decision.RETRY, record_id=planner)
        row = await pool.fetchrow('SELECT * FROM step_attempts WHERE id=$1', first)
        assert (row['worker_runs'], row['worker_record_id'], row['profile_id']) == (2, fallback, 'two')
        assert (row['run_outcome'], row['review_runs'], row['review_outcome'], row['blocking_findings']) == (
            'timeout', 3, 'not_passed', 2)
        assert row['failure_kind'] == 'review_rejected'
        assert await pool.fetchval('SELECT count(*) FROM llm_records WHERE attempt_id=$1', first) == 4
        assert 'attempt_id' not in await llm_records.get_record(review)
        assert (await llm_records.get_record(review, include_attempt_id=True))['attempt_id'] == first
        assert all('attempt_id' not in row for row in await llm_records.list_records())
        retry = await attempts.open_attempt('t', 's', 's1', 1, Trigger.RETRY)
        await attempts.close_attempts('t', failure_kind=FailureKind.STOPPED, step_key='s1')
        restart = await attempts.open_attempt('t', 's', 's1', 1, Trigger.RESTART)
        assert len({first, retry, restart}) == 3
        assert await pool.fetchval('SELECT max(attempt) FROM step_attempts') == 3
        await attempts.review_finished(restart, False, 1)
        await attempts.decide_attempt(restart, Decision.ACCEPT, adjudicated=True)
        row = await pool.fetchrow('SELECT * FROM step_attempts WHERE id=$1', restart)
        assert row['adjudicated'] and row['review_outcome'] == 'passed_by_adjudication'
        assert row['failure_kind'] is None and row['decided_at'] is not None
        assert await pool.fetchval('SELECT count(*) FROM step_attempts WHERE decision IS NULL') == 0
        await pool.execute('DELETE FROM task_steps WHERE id=$1', 's')
        assert await pool.fetchval('SELECT count(*) FROM step_attempts WHERE step_id IS NULL AND step_key=$1', 's1') == 3

    run_db(test, monkeypatch, setup=seed)


def test_dispatch_resolves_current_run_and_same_attempt_without_backfill(monkeypatch):
    async def test(pool):
        assert await attempts.dispatch_attempt('t', 's', 's1', same_attempt=True) is None
        first = await attempts.dispatch_attempt('t', 's', 's1')
        assert first is not None
        assert await attempts.dispatch_attempt('t', 's', 's1', same_attempt=True) == first
        await attempts.decide_attempt(first, Decision.RETRY)
        retry = await attempts.dispatch_attempt('t', 's', 's1')
        assert await pool.fetchval('SELECT trigger FROM step_attempts WHERE id=$1', retry) == 'retry'
        await attempts.close_attempts('t', failure_kind=FailureKind.STOPPED)
        await pool.execute("UPDATE tasks SET run_no=2 WHERE id='t'")
        restarted = await attempts.dispatch_attempt('t', 's', 's1', Trigger.RESTART)
        assert await attempts.resolve_attempt('t', 's1') == restarted
        assert await pool.fetchval('SELECT trigger FROM step_attempts WHERE id=$1', restarted) == 'restart'
        await attempts.review_finished(restarted, False, 2, required=False)
        assert await pool.fetchval('SELECT review_outcome FROM step_attempts WHERE id=$1', restarted) is None
        assert await pool.fetchval('SELECT review_runs FROM step_attempts WHERE id=$1', restarted) == 1

    run_db(test, monkeypatch, setup=seed)


def test_resume_recovers_a_pending_replanned_step_from_its_creation_boundary(monkeypatch):
    async def test(pool):
        # All original steps predate the first dispatch. A replan adds a new step afterwards.
        await pool.execute("INSERT INTO task_steps(id,task_id,step_index,instruction,step_key) "
                           "VALUES ('original','t',1,'Synthetic original step','s2')")
        first = await attempts.dispatch_attempt('t', 's', 's1')
        await attempts.decide_attempt(first, Decision.REPLAN)
        await pool.execute("INSERT INTO task_steps(id,task_id,step_index,instruction,step_key) "
                           "VALUES ('added','t',2,'Synthetic replanned step','s3')")
        # With no in-memory trigger after a service restart, persisted typed times recover it.
        added = await attempts.dispatch_attempt('t', 'added', 's3')
        original = await attempts.dispatch_attempt('t', 'original', 's2')
        assert await pool.fetchval('SELECT trigger FROM step_attempts WHERE id=$1', added) == 'replanned'
        assert await pool.fetchval('SELECT trigger FROM step_attempts WHERE id=$1', original) == 'first'
        await pool.execute("UPDATE tasks SET run_no=2 WHERE id='t'")
        restarted = await attempts.dispatch_attempt('t', 's', 's1')
        assert await pool.fetchval('SELECT trigger FROM step_attempts WHERE id=$1', restarted) == 'restart'

    run_db(test, monkeypatch, setup=seed)


@pytest.mark.parametrize('trigger', [None, Trigger.FIRST, Trigger.RESTART])
@pytest.mark.parametrize('history', ['same_step', 'unstarted', 'start_over'])
def test_restart_requires_an_attempt_for_the_same_step_in_an_earlier_run(monkeypatch, trigger, history):
    async def test(pool):
        if history != 'unstarted':
            first = await attempts.dispatch_attempt('t', 's', 's1')
            await attempts.decide_attempt(first, Decision.FAIL)
        if history == 'start_over':
            await pool.execute("DELETE FROM task_steps WHERE id='s'")
            await pool.execute("INSERT INTO task_steps(id,task_id,step_index,instruction,step_key) "
                               "VALUES ('replacement','t',0,'Synthetic replacement step','s1')")
        await pool.execute("UPDATE tasks SET run_no=2 WHERE id='t'")
        step_id = 'replacement' if history == 'start_over' else 's'
        current = await attempts.dispatch_attempt('t', step_id, 's1', trigger)
        expected = 'restart' if history == 'same_step' else 'first'
        assert await pool.fetchval('SELECT trigger FROM step_attempts WHERE id=$1', current) == expected

    run_db(test, monkeypatch, setup=seed)


@pytest.mark.parametrize("decision,run,review,expected", [
    (Decision.RETRY, RunResult.COMPLETED, ReviewResult.NOT_PASSED, FailureKind.REVIEW_REJECTED),
    (Decision.REPLAN, RunResult.CONTRACT, ReviewResult.NOT_PASSED, FailureKind.REVIEW_REJECTED),
    (Decision.FAIL, RunResult.COMPLETED, ReviewResult.NOT_PASSED, FailureKind.REVIEW_REJECTED),
    *[(Decision.FAIL, RunResult(value), None, FailureKind(value)) for value in (
        "contract", "files_mismatch", "reported_failed", "run_error", "timeout", "run_cap", "crash")],
    (Decision.ACCEPT, RunResult.CONTRACT, ReviewResult.NOT_PASSED, None),
    (Decision.RETRY, RunResult.COMPLETED, ReviewResult.PASSED, None),
    (Decision.TASK_ENDED, RunResult.COMPLETED, None, None),
    (Decision.FAIL, None, None, None),
])
def test_decision_persists_failure_kind_from_typed_results(monkeypatch, decision, run, review, expected):
    async def test(pool):
        attempt = await attempts.open_attempt('t', 's', 's1', 1, Trigger.FIRST)
        await pool.execute('UPDATE step_attempts SET run_outcome=$2,review_outcome=$3 WHERE id=$1',
                           attempt, run, review)
        await step_attempts.decide_attempt(attempt, decision, pool=pool)
        assert await pool.fetchval('SELECT failure_kind FROM step_attempts WHERE id=$1', attempt) == expected
        if review is None:
            assert await pool.fetchval('SELECT review_outcome FROM step_attempts WHERE id=$1', attempt) == 'none'

    run_db(test, monkeypatch, setup=seed)


def test_late_blocking_seat_and_its_record_move_to_the_reopened_attempt(monkeypatch):
    async def test(pool):
        accepted = await attempts.dispatch_attempt('t', 's', 's1')
        reviewer = await llm_records.start_record(agent_id='reviewer-late', task_id='t')
        await attempts.link_record(accepted, reviewer)
        fallback = await llm_records.start_record(agent_id='reviewer-late', task_id='t')
        await attempts.link_record(accepted, fallback)
        await attempts.decide_attempt(accepted, Decision.ACCEPT)
        reopened = await attempts.dispatch_attempt('t', 's', 's1', Trigger.REOPEN_LATE_REVIEW)
        await attempts.link_review_record(reopened, 't', 'reviewer-late')
        await attempts.review_finished(reopened, False, 1)
        assert await pool.fetchval('SELECT attempt_id FROM llm_records WHERE id=$1', reviewer) == reopened
        assert await pool.fetchval('SELECT attempt_id FROM llm_records WHERE id=$1', fallback) == reopened
        assert await pool.fetchval('SELECT review_runs FROM step_attempts WHERE id=$1', accepted) == 0
        assert await pool.fetchval('SELECT review_runs FROM step_attempts WHERE id=$1', reopened) == 1
        assert await pool.fetchval('SELECT trigger FROM step_attempts WHERE id=$1', reopened) == 'reopen_late_review'

    run_db(test, monkeypatch, setup=seed)


@pytest.mark.parametrize('failure', [RunFailure.TIMEOUT, RunFailure.RUN_CAP, RunFailure.CRASH, None])
def test_typed_run_failure_survives_settlement(monkeypatch, failure):
    async def test(pool):
        attempt = await attempts.open_attempt('t', 's', 's1', 1, Trigger.FIRST)
        await attempts.worker_finished(attempt, RunOutcome.RUN_FAILED, failure)
        await attempts.decide_attempt(attempt, Decision.FAIL)
        expected = failure.value if failure else 'run_error'
        assert await pool.fetchval('SELECT failure_kind FROM step_attempts WHERE id=$1', attempt) == expected

    run_db(test, monkeypatch, setup=seed)


def test_cancel_closes_only_open_attempts_in_the_current_run(monkeypatch):
    async def test(pool):
        old = await attempts.open_attempt('t', 's', 's1', 1, Trigger.FIRST)
        await pool.execute("UPDATE tasks SET run_no=2 WHERE id='t'")
        current = await attempts.open_attempt('t', 's', 's1', 2, Trigger.FIRST)
        await attempts.close_attempts('t', failure_kind=FailureKind.TASK_BUDGET)
        rows = {r['id']: r for r in await pool.fetch('SELECT * FROM step_attempts')}
        assert rows[old]['decision'] is None
        assert rows[current]['decision'] == 'task_ended'
        assert rows[current]['failure_kind'] == 'task_budget'

    run_db(test, monkeypatch, setup=seed)


def test_advisor_consults_retain_the_typed_attempt_link(monkeypatch):
    from looper.agents import advisor
    from looper.db import advisor_advice, agent_spawns

    async def test(pool):
        attempt = await attempts.dispatch_attempt('t', 's', 's1')
        brief = advisor.Brief('t', 's', 1, advisor.Source.OPTIONAL, attempt_id=attempt)
        spawn = await advisor.open_consult(brief, budget_usd=1)
        restored = advisor._brief(await agent_spawns.get(spawn))
        assert restored.attempt_id == attempt
        await advisor_advice.record_question(spawn_id=spawn, task_id='t', step_id='s', attempt=1,
                                            source='optional', kind='diagnose', question={})
        await attempts.link_advice(restored.attempt_id, spawn)
        row = await advisor_advice.get(spawn)
        assert (row['attempt_id'], row['attempt']) == (attempt, 1)
        # A checkpoint asks before the first Worker opens an attempt; dispatch fills its link afterwards.
        checkpoint = await advisor.open_consult(advisor.Brief('t', 's', 1, advisor.Source.CHECKPOINT), budget_usd=1)
        await advisor_advice.record_question(spawn_id=checkpoint, task_id='t', step_id='s', attempt=1,
                                            source='checkpoint', kind='plan_review', question={})
        assert (await advisor_advice.get(checkpoint))['attempt_id'] is None
        await attempts.link_advice(attempt, checkpoint)
        assert (await advisor_advice.get(checkpoint))['attempt_id'] == attempt

    run_db(test, monkeypatch, setup=seed)
