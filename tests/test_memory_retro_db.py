"""Durable retrospective ranges, fenced attempts and project-scoped application."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from looper.agents.memory_lessons_model import parse_settings
from looper.db import memory_retro as retro
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")
NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)
SETTINGS = parse_settings({"enabled": True, "retro_budget_usd": .10})


async def configure(pool, *, auto=False, cap=60):
    import json
    settings = parse_settings({"enabled": True, "auto_activate": auto, "retro_budget_usd": .10,
                               "project_cap": cap})
    await pool.execute("INSERT INTO system_settings(key,value) VALUES('memory_lessons',$1) "
                       "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", json.dumps(settings.to_json()))
    await pool.execute("INSERT INTO projects(id,name,memory_lessons) VALUES('retro-project','Retro','on') "
                       "ON CONFLICT DO NOTHING")
    async with pool.acquire() as conn:
        await retro.activate(conn, now=NOW)
        await retro.bootstrap(conn, now=NOW)
    return settings


async def task(pool, number, *, state="completed", taint=None, count=3, project="retro-project"):
    tid = f"retro-task-{number}"
    await pool.execute("INSERT INTO tasks(id,requirement,project_id,status,untrusted_inputs,updated_at,completed_at) "
                       "VALUES($1,'Synthetic retrospective',$2,$3,$4,$5,$5)", tid, project, state,
                       [] if taint is None else taint, NOW + timedelta(seconds=1))
    ids = []
    for i in range(count):
        ids.append(await pool.fetchval("INSERT INTO task_journal(task_id,step_id,kind,summary) "
                                       "VALUES($1,$2,'decision','Validated evidence') RETURNING id", tid, str(i)))
    return tid, ids


async def enqueue(pool, tid, settings=SETTINGS, now=NOW):
    async with pool.acquire() as conn:
        return await retro.enqueue_finished(conn, tid, "completed", settings=settings, now=now)


async def claim(pool, settings=SETTINGS, now=NOW):
    async with pool.acquire() as conn:
        return await retro.claim_next(conn, settings=settings, now=now)


def reply(ids, statement="Check the generated artifact", **fields):
    item = SimpleNamespace(statement=statement, why="The check caught the fault", keywords=("check",),
                           evidence=("J1",), same_as=None, supersedes=None)
    for name, value in fields.items():
        setattr(item, name, value)
    return SimpleNamespace(lessons=(item,), counts={}, note="Validated check"), SimpleNamespace(
        journal_ids={"J1": ids[0]}, lesson_ids={})


async def apply(pool, run, answer, inp, settings=SETTINGS):
    async with pool.acquire() as conn:
        return await retro.apply_reply(conn, run["id"], run["lease_token"], answer, inp,
                                       settings=settings, now=NOW, cost_usd=.01)


def test_finished_ranges_are_durable_and_do_not_replay():
    async def check(pool):
        await configure(pool)
        tid, ids = await task(pool, 1)
        first = await enqueue(pool, tid)
        assert (first["journal_from_id"], first["journal_to_id"]) == (ids[0], ids[-1])
        assert await enqueue(pool, tid) is None
        await pool.execute("DELETE FROM memory_retro_runs")
        assert await enqueue(pool, tid) is None
        await pool.execute("INSERT INTO task_journal(task_id,step_id,kind) VALUES($1,'later','decision')", tid)
        later = await enqueue(pool, tid)
        assert (later["state"], later["skip_reason"]) == ("skipped", "few_entries")
        cancel, _ = await task(pool, 2, state="cancelled")
        assert await enqueue(pool, cancel) is None
        failed, _ = await task(pool, 3, state="failed")
        assert await enqueue(pool, failed) is not None
    run_db(check)


def test_activation_excludes_history_but_recovers_new_terminal_tasks():
    async def check(pool):
        await pool.execute("INSERT INTO projects(id,name,memory_lessons) VALUES('retro-project','Retro','on')")
        old, _ = await task(pool, "history")
        await pool.execute("UPDATE tasks SET updated_at=$2,completed_at=$2 WHERE id=$1", old, NOW-timedelta(days=1))
        await configure(pool)
        fresh, _ = await task(pool, "fresh")
        async with pool.acquire() as conn:
            assert await retro.recover_finished(conn, settings=SETTINGS, now=NOW+timedelta(seconds=2)) == 1
        assert {r[0] for r in await pool.fetch("SELECT task_id FROM memory_retro_runs")} == {fresh}
        assert await enqueue(pool, old) is None
    run_db(check)


def test_claim_lease_fences_old_result_and_preserves_attempt_budget():
    async def check(pool):
        await configure(pool)
        tid, ids = await task(pool, 1)
        await enqueue(pool, tid)
        first = await claim(pool)
        assert first["budget_usd"] == .10
        assert await claim(pool) is None
        second = await claim(pool, now=NOW+timedelta(minutes=3))
        assert second["attempts"] == 2 and second["lease_token"] != first["lease_token"]
        answer, inp = reply(ids)
        assert await apply(pool, first, answer, inp) is None
        async with pool.acquire() as conn:
            assert await retro.settle_attempt(conn, second["id"], second["lease_token"], cost_usd=None,
                                              failure="parse", now=NOW+timedelta(minutes=3))
        assert await pool.fetchval("SELECT state FROM memory_retro_runs") == "failed"
        assert await pool.fetchval("SELECT sum(reserved_usd) FROM memory_retro_spend") == Decimal('.20')
        assert set(r[0] for r in await pool.fetch("SELECT state FROM memory_retro_spend")) == {"unknown"}
    run_db(check)


def test_daily_budget_is_reserved_across_consumers_and_survives_run_deletion():
    async def check(pool):
        await configure(pool)
        settings = parse_settings({"enabled": True,"retro_budget_usd": .1,"daily_budget_usd": .15})
        import json
        await pool.execute("UPDATE system_settings SET value=$1 WHERE key='memory_lessons'", json.dumps(settings.to_json()))
        for n in range(2):
            tid, _ = await task(pool, n)
            await enqueue(pool, tid)
        import asyncio
        results = await asyncio.gather(claim(pool, settings), claim(pool, settings))
        assert sum(result is not None for result in results) == 1
        assert await pool.fetchval("SELECT skip_reason FROM memory_retro_runs WHERE state='skipped'") == 'budget'
        await pool.execute("DELETE FROM memory_retro_runs")
        assert await pool.fetchval("SELECT reserved_usd FROM memory_retro_spend") == Decimal('.1')
    run_db(check)


@pytest.mark.parametrize("taint", [[], ["leads"], ["intent"]])
def test_source_trust_cannot_be_laundered_by_reinforcement(taint):
    async def check(pool):
        settings = await configure(pool, auto=True)
        tid, ids = await task(pool, 1, taint=taint)
        await enqueue(pool, tid)
        run = await claim(pool)
        answer, inp = reply(ids)
        await apply(pool, run, answer, inp, settings)
        row = await pool.fetchrow("SELECT * FROM memory_candidates")
        assert row["source_class"] == ("external" if taint else "agent")
        assert row["state"] == ("held" if taint else "in_effect")
        if taint:
            assert row["reason"] == "external_source"
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_sources") == 1
        assert await pool.fetchval("SELECT count(*) FROM activity WHERE kind='memory_retro'") == 1
    run_db(check)


def test_retro_apply_is_atomic_and_project_scoped():
    async def check(pool):
        settings = await configure(pool, auto=True, cap=1)
        tid, ids = await task(pool, 1)
        await enqueue(pool, tid)
        run = await claim(pool)
        answer, inp = reply(ids)
        await apply(pool, run, answer, inp, settings)
        assert await apply(pool, run, answer, inp, settings) is None
        second, more = await task(pool, 2)
        await enqueue(pool, second)
        other = await claim(pool)
        another, inp2 = reply(more, "A separate reliable check")
        await apply(pool, other, another, inp2, settings)
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates WHERE state='in_effect'") == 1
        assert await pool.fetchval("SELECT reason FROM memory_candidates WHERE state='held'") == 'project_cap'
    run_db(check)


def test_later_task_reinforcement_is_counted_once():
    async def check(pool):
        settings = await configure(pool, auto=True)
        tid, ids = await task(pool, 1)
        await enqueue(pool, tid)
        run = await claim(pool)
        answer, inp = reply(ids)
        await apply(pool, run, answer, inp, settings)
        candidate = await pool.fetchval("SELECT id FROM memory_candidates")
        for n in range(2):
            await pool.execute("INSERT INTO task_journal(task_id,step_id,kind) SELECT $1,x,'decision' "
                               "FROM unnest($2::text[]) x", tid, [f"extra-{n}-{i}" for i in range(3)])
            await enqueue(pool, tid)
            run = await claim(pool)
            answer, inp = reply([await pool.fetchval("SELECT max(id) FROM task_journal WHERE task_id=$1", tid)])
            await apply(pool, run, answer, inp, settings)
        assert await pool.fetchval("SELECT COALESCE(sum(reinforced),0) FROM memory_lesson_stats") == 0
        later, ids2 = await task(pool, 2)
        await enqueue(pool, later)
        run = await claim(pool)
        answer, inp = reply(ids2)
        await apply(pool, run, answer, inp, settings)
        assert await pool.fetchval("SELECT reinforced FROM memory_lesson_stats WHERE candidate_id=$1", candidate) == 1
        await pool.execute("UPDATE memory_lesson_stats SET injected=7,cited=3,wrong_tasks=1 WHERE candidate_id=$1", candidate)
        await pool.execute("DELETE FROM memory_retro_runs")
        await pool.execute("INSERT INTO task_journal(task_id,step_id,kind) SELECT $1,x,'decision' "
                           "FROM unnest(ARRAY['retained-1','retained-2','retained-3']) x", later)
        await enqueue(pool, later)
        run = await claim(pool)
        answer, inp = reply([await pool.fetchval("SELECT max(id) FROM task_journal WHERE task_id=$1", later)])
        await apply(pool, run, answer, inp, settings)
        stats = await pool.fetchrow("SELECT reinforced,injected,cited,wrong_tasks FROM memory_lesson_stats WHERE candidate_id=$1", candidate)
        assert tuple(stats) == (1,7,3,1)
        assert await pool.fetchval("SELECT count(*) FROM memory_lesson_reinforcements WHERE candidate_id=$1", candidate) == 1
    run_db(check)


def test_external_same_as_and_newly_external_inputs_never_mutate_agent_lesson():
    async def check(pool):
        settings = await configure(pool, auto=True)
        tid, ids = await task(pool, 1)
        await enqueue(pool, tid)
        first = await claim(pool)
        answer, inp = reply(ids)
        await apply(pool, first, answer, inp, settings)
        old = dict(await pool.fetchrow("SELECT * FROM memory_candidates"))
        second, more = await task(pool, 2)
        await enqueue(pool, second)
        run = await claim(pool)
        answer, inp = reply(more, same_as="L1")
        inp.lesson_ids = {"L1": old["id"]}
        await pool.execute("UPDATE tasks SET untrusted_inputs=ARRAY['leads'] WHERE id=$1", second)
        await apply(pool, run, answer, inp, settings)
        after = dict(await pool.fetchrow("SELECT * FROM memory_candidates WHERE id=$1", old["id"]))
        assert after == old
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_sources WHERE candidate_id=$1", old["id"]) == 1
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates WHERE state='held' AND reason='external_source'") == 1
        assert await pool.fetchval("SELECT count(*) FROM memory_lesson_stats") == 0
    run_db(check)


def test_supersedes_reuses_overwrite_gate():
    async def check(pool):
        settings = await configure(pool, auto=True)
        tid, ids = await task(pool, 1)
        await enqueue(pool, tid)
        run = await claim(pool)
        answer, inp = reply(ids, "Use `check --timeout 30` at src/check.py")
        await apply(pool, run, answer, inp, settings)
        old = await pool.fetchval("SELECT id FROM memory_candidates")
        for number, statement, expected in (
            (2, "Use check at src/check.py", "held"),
            (3, "Use `check --timeout 30` at src/check.py after validating inputs", "in_effect")):
            tid, ids = await task(pool, number)
            await enqueue(pool, tid)
            run = await claim(pool)
            answer, inp = reply(ids, statement, supersedes="L1")
            inp.lesson_ids = {"L1": old}
            await apply(pool, run, answer, inp, settings)
            new = await pool.fetchrow("SELECT state,reason FROM memory_candidates ORDER BY id DESC LIMIT 1")
            assert new["state"] == expected
            assert await pool.fetchval("SELECT state FROM memory_candidates WHERE id=$1", old) == (
                "in_effect" if expected == "held" else "superseded")
            if expected == "held":
                assert new["reason"] == "overwrite_gate"
    run_db(check)


def test_apply_rollback_tombstones_and_cross_project_targets(monkeypatch):
    async def check(pool):
        settings = await configure(pool, auto=True)
        tid, ids = await task(pool, 1)
        await enqueue(pool, tid)
        run = await claim(pool)
        answer, inp = reply(ids)
        async def broken(*args, **kwargs):
            raise RuntimeError("synthetic transactional failure")
        with monkeypatch.context() as scoped:
            scoped.setattr(retro, "_event", broken)
            with pytest.raises(RuntimeError, match="synthetic transactional failure"):
                await apply(pool, run, answer, inp, settings)
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 0
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_sources") == 0
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_events") == 0
        assert await pool.fetchval("SELECT state FROM memory_retro_runs") == 'running'
        assert await pool.fetchval("SELECT state FROM memory_retro_spend") == 'reserved'
        from looper.agents.memory_model import fingerprint, Kind
        await pool.execute("INSERT INTO memory_tombstones(fingerprint,kind,destination,deleted_via) "
                           "VALUES($1,'lesson','looper','dashboard')", fingerprint(Kind.LESSON, answer.lessons[0].statement))
        result = await apply(pool, run, answer, inp, settings)
        assert result["counts"]["tombstoned"] == 1
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 0
        other, ids = await task(pool, 2)
        await enqueue(pool, other)
        run = await claim(pool)
        answer, inp = reply(ids, "Different check", same_as="L1")
        inp.lesson_ids = {"L1": 999999}
        result = await apply(pool, run, answer, inp, settings)
        assert result["counts"]["invalid"] == 1
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 0
    run_db(check)


def test_concurrent_claims_and_creations_preserve_project_cap():
    import asyncio
    async def check(pool):
        settings = await configure(pool, auto=True, cap=1)
        tasks = [await task(pool, number) for number in range(2)]
        for tid, _ in tasks:
            await enqueue(pool, tid)
        runs = await asyncio.gather(claim(pool), claim(pool))
        assert len({row["lease_token"] for row in runs}) == 2
        by_task = {tid: reply(ids, "Reliable check " + str(i)) for i, (tid, ids) in enumerate(tasks)}
        answers = [by_task[run["task_id"]] for run in runs]
        await asyncio.gather(*(apply(pool, run, *answer, settings) for run, answer in zip(runs, answers)))
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates WHERE state='in_effect'") == 1
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates WHERE reason='project_cap'") == 1
        assert await pool.fetchval("SELECT count(*) FROM memory_retro_runs WHERE state='done'") == 2
    run_db(check)


def test_failure_notification_claim_counts_distinct_tasks_and_resets_on_success():
    async def check(pool):
        await configure(pool)
        async with pool.acquire() as conn:
            for number in range(3):
                tid, _ = await task(pool, number)
                await enqueue(pool, tid)
                for attempt in range(2):
                    run = await claim(pool)
                    await retro.settle_attempt(conn, run["id"], run["lease_token"], cost_usd=0,
                                               failure="parse", now=NOW)
                if number < 2:
                    assert await retro.claim_failure_notice(conn) is None
            assert len(await retro.claim_failure_notice(conn)) == 3
            assert await retro.claim_failure_notice(conn) is None
        assert await pool.fetchval("SELECT count(*) FROM activity WHERE summary LIKE '%failed (parse)%'") == 3
        tid, ids = await task(pool, 4)
        await enqueue(pool, tid)
        run = await claim(pool)
        answer, inp = reply(ids)
        await apply(pool, run, answer, inp)
        assert await pool.fetchval("SELECT failure_tasks FROM memory_retro_activation") == []
    run_db(check)


def test_claim_rechecks_current_permissions_and_never_spends_when_disabled():
    async def check(pool):
        await configure(pool)
        tid, _ = await task(pool, 1)
        await enqueue(pool, tid)
        await pool.execute("UPDATE system_settings SET value='{\"enabled\":false}' WHERE key='memory_lessons'")
        assert await claim(pool, SETTINGS) is None
        assert await pool.fetchval("SELECT skip_reason FROM memory_retro_runs") == 'disabled'
        assert await pool.fetchval("SELECT count(*) FROM memory_retro_spend") == 0
    run_db(check)


@pytest.mark.parametrize("stored,mode", [
    (None,"on"), ('{"enabled":false}',"on"), ('{"enabled":true}',"off"),
    ('{"enabled":true}',None), ('{broken',"on"),
])
def test_disabled_enqueue_preserves_all_durable_state(stored, mode):
    async def check(pool):
        await pool.execute("INSERT INTO projects(id,name,memory_lessons) VALUES('retro-project','Retro',$1)", mode)
        tid, _ = await task(pool, 1)
        if stored is not None:
            await pool.execute("INSERT INTO system_settings(key,value) VALUES('memory_lessons',$1)", stored)
        assert await enqueue(pool, tid) is None
        for table in ("memory_retro_runs","memory_retro_cursors","memory_retro_spend", "memory_retro_activation",
                      "memory_candidates", "memory_candidate_sources", "memory_candidate_events"):
            assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0, table
        # Positive control: exactly the same task enqueues after both gates become valid.
        await configure(pool)
        await pool.execute("UPDATE projects SET memory_lessons='on' WHERE id='retro-project'")
        assert (await enqueue(pool, tid))["state"] == 'pending'
        assert await pool.fetchval("SELECT count(*) FROM memory_retro_runs") == 1
    run_db(check)


@pytest.mark.parametrize("setting_change", ["disabled", "project_off"])
def test_apply_rechecks_gate_and_settles_cost_without_creating_candidates(setting_change):
    async def check(pool):
        settings = await configure(pool, auto=True)
        tid, ids = await task(pool, 1)
        await enqueue(pool, tid)
        run = await claim(pool)
        answer, inp = reply(ids)
        if setting_change == 'disabled':
            await pool.execute("UPDATE system_settings SET value='{\"enabled\":false}' WHERE key='memory_lessons'")
        else:
            await pool.execute("UPDATE projects SET memory_lessons='off'")
        assert (await apply(pool, run, answer, inp, settings))["skip_reason"] == setting_change
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 0
        assert await pool.fetchval("SELECT state FROM memory_retro_spend") == 'settled'
    run_db(check)


def test_historical_terminal_edit_does_not_cross_activation_boundary():
    async def check(pool):
        await pool.execute("INSERT INTO projects(id,name,memory_lessons) VALUES('retro-project','Retro','on')")
        tid, _ = await task(pool, 'old')
        await pool.execute("UPDATE tasks SET completed_at=$2,updated_at=$2 WHERE id=$1", tid, NOW-timedelta(days=2))
        async with pool.acquire() as conn:
            await retro.activate(conn, now=NOW)
            await pool.execute("UPDATE tasks SET title='Edited title',updated_at=$2 WHERE id=$1", tid, NOW+timedelta(days=1))
            await retro.bootstrap(conn, now=NOW+timedelta(days=1))
        await configure(pool)
        async with pool.acquire() as conn:
            assert await retro.recover_finished(conn, settings=SETTINGS, now=NOW+timedelta(days=1)) == 0
        assert await enqueue(pool, tid) is None
        assert await pool.fetchval("SELECT count(*) FROM memory_retro_runs") == 0
    run_db(check)


def test_superseded_and_held_fingerprints_cannot_be_reactivated_by_another_item():
    async def check(pool):
        settings = await configure(pool, auto=True)
        tid, ids = await task(pool, 1)
        await enqueue(pool, tid)
        run = await claim(pool)
        answer, inp = reply(ids, "Validate the generated artifact")
        await apply(pool, run, answer, inp, settings)
        old = await pool.fetchval("SELECT id FROM memory_candidates")
        tid, ids = await task(pool, 2)
        await enqueue(pool, tid)
        run = await claim(pool)
        answer, inp = reply(ids, "Validate the generated artifact before publishing", supersedes="L1")
        old_item, _ = reply(ids, "Validate the generated artifact")
        answer.lessons = answer.lessons + old_item.lessons
        inp.lesson_ids = {"L1": old}
        result = await apply(pool, run, answer, inp, settings)
        assert result["counts"]["invalid"] == 1
        assert await pool.fetchval("SELECT state FROM memory_candidates WHERE id=$1", old) == 'superseded'
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates WHERE state='in_effect'") == 1
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_sources WHERE candidate_id=$1", old) == 1
        # A held fingerprint requires an operator transition, never a fresh system-created active duplicate.
        await pool.execute("UPDATE memory_candidates SET state='held',reason='shadow' WHERE state='in_effect'")
        tid, ids = await task(pool, 3)
        await enqueue(pool, tid)
        run = await claim(pool)
        answer, inp = reply(ids, "Validate the generated artifact before publishing")
        result = await apply(pool, run, answer, inp, settings)
        assert result["counts"]["invalid"] == 1
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 2
    run_db(check)


def test_intent_link_without_taint_is_external_and_expired_fingerprint_stays_inactive():
    async def check(pool):
        settings = await configure(pool, auto=True)
        tid, ids = await task(pool, 1)
        await pool.execute("INSERT INTO event_sources(id,kind,name) VALUES('retro-source','lark_user','Synthetic source')")
        await pool.execute("INSERT INTO intent_types(key,version) VALUES('retro_intent',1)")
        await pool.execute("INSERT INTO intents(source_id,type_key,type_version,title,confidence,chat_id,task_id) "
                           "VALUES('retro-source','retro_intent',1,'Synthetic intent','high','synthetic-chat',$1)", tid)
        await enqueue(pool, tid)
        run = await claim(pool)
        answer, inp = reply(ids)
        await apply(pool, run, answer, inp, settings)
        row = await pool.fetchrow("SELECT state,reason,source_class FROM memory_candidates")
        assert tuple(row) == ('held','external_source','external')
        # An independently trusted task may produce an active row despite an external held candidate.
        tid, ids = await task(pool, 2)
        await enqueue(pool, tid)
        run = await claim(pool)
        answer, inp = reply(ids)
        await apply(pool, run, answer, inp, settings)
        await pool.execute("UPDATE memory_candidates SET state='expired',valid_until=$1 WHERE state='in_effect'", NOW-timedelta(days=1))
        tid, ids = await task(pool, 3)
        await enqueue(pool, tid)
        run = await claim(pool)
        answer, inp = reply(ids)
        result = await apply(pool, run, answer, inp, settings)
        assert result["counts"]["invalid"] == 1
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates WHERE state='in_effect'") == 0
    run_db(check)


def test_retro_input_excludes_operator_only_external_held_candidates():
    async def check(pool):
        settings = await configure(pool, auto=True)
        external, ids = await task(pool, 1, taint=['leads'])
        await enqueue(pool, external)
        run = await claim(pool)
        answer, inp = reply(ids, 'External candidate text')
        await apply(pool, run, answer, inp, settings)
        trusted, ids = await task(pool, 2)
        await enqueue(pool, trusted)
        run = await claim(pool)
        async with pool.acquire() as conn:
            data = await retro.load_input(conn, run, now=NOW, settings=settings)
        assert data['held'] == []
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates WHERE reason='external_source'") == 1
    run_db(check)
