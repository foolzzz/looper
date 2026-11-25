"""Only a re-dispatched interrupted attempt gets the fixed recovery instruction."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.tiers import worker
from looper.db import step_attempts
from tests.run_handle_support import ScriptedRun
from test_worker_run_handles import _pool, _item, _events
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401

KEY = "worker.interrupted_hint.enabled"
LINE = ("This step was interrupted before (restart not adopted); the workspace may hold partial changes from that run. "
        "Check git status and the files you will touch before continuing, and do not repeat external side effects "
        "(pushes, comments, messages) without checking whether they already happened.")


@pytest.mark.parametrize("case", ["interrupted", "first", "failure", "new_attempt", "reviewer", "off", "invalid", "unset",
                                 "read_error"])
def test_interruption_hint_is_scoped_to_the_previous_worker_run(tmp_path, monkeypatch, agent_db, caplog, case):
    previous = None if case in ("first", "new_attempt") else {
        "status": "failed" if case == "failure" else "interrupted", "error": "not_adopted", "metadata": {}}
    lookup = AsyncMock(return_value=previous)
    monkeypatch.setattr(step_attempts, "previous_worker_run", lookup, raising=False)
    agent_db.get_settings.return_value = {KEY: {"off": "false", "invalid": "yes"}.get(case, "true")}
    if case == "unset":
        agent_db.get_settings.return_value = {}
    if case == "read_error":
        monkeypatch.setattr(worker.worker_interruption, "_read_warning_logged", False)

        async def read(keys):
            if KEY in keys:
                raise RuntimeError("settings unavailable")
            return {}

        agent_db.get_settings.side_effect = read

    async def scenario():
        async with _pool(tmp_path, monkeypatch, ScriptedRun(_events()), ScriptedRun(_events()),
                         ScriptedRun(_events())) as (pool, _, _):
            item = _item(tmp_path)
            item.payload.update(attempt_id=7, dependencies=[{"step": "seed", "summary": "ready"}], findings="A finding")
            original = dict(item.payload)
            ctx = await pool._prepare(item, is_verify=case == "reviewer")
            if case == "read_error":
                repeated = await pool._prepare(item)
                assert isinstance(repeated, worker.RunContext) and repeated.prompt == ctx.prompt
                warnings = [record for record in caplog.records
                            if record.name == "looper.agents.worker_interruption" and record.levelname == "WARNING"]
                assert len(warnings) == 1 and "disabled" in warnings[0].message
                agent_db.get_settings.side_effect = None
            agent_db.get_settings.return_value = {}
            baseline = await pool._prepare(item, is_verify=case == "reviewer")
            assert isinstance(ctx, worker.RunContext) and isinstance(baseline, worker.RunContext)
            assert item.payload == original  # another preparation/fallback must not accumulate the line
            assert ctx.system_prompt == baseline.system_prompt
            assert ctx.command == baseline.command
            assert ctx.launch.binding_sha256 == baseline.launch.binding_sha256
            if case == "interrupted":
                assert ctx.prompt.splitlines()[1] == LINE
                assert ctx.prompt.count(LINE) == 1
                assert ctx.prompt.replace("\n" + LINE, "", 1) == baseline.prompt
            else:
                assert ctx.prompt == baseline.prompt
            if case != "reviewer":
                assert baseline.prompt == ("Start your step (the Task section of your instructions).\n\n"
                    "What the steps it depends on reported (their Workers' words, to use as data):\n"
                    "- seed: ready\n\nA finding")
            else:
                assert baseline.prompt == ("Review the step in the Task section of your instructions. "
                                          "The Worker's report, to check:\nFiles changed: {}")
            if case in ("off", "invalid", "unset", "reviewer", "read_error"):
                lookup.assert_not_awaited()
            else:
                lookup.assert_awaited_once_with(7, "t1", "s1")

    asyncio.run(scenario())


@pytest.mark.parametrize("error,metadata,reason", [
    ("not_adopted", {}, "restart not adopted"),
    ("the run could not be adopted after a restart: launch_changed", {}, "restart not adopted"),
    ("synthetic secret in an error", {"interruption_reason": "deploy drain"}, "deploy drain"),
    ("synthetic secret in an error", {"interruption_reason": "synthetic secret"}, "run stopped"),
])
def test_interruption_reason_is_typed_and_never_copies_error_text(error, metadata, reason):
    from looper.agents.worker_interruption import InterruptionReason, reason_for

    actual = reason_for({"error": error, "metadata": metadata})
    assert isinstance(actual, InterruptionReason)
    assert actual.value == reason


def test_previous_worker_run_uses_only_the_same_attempt(tmp_path, monkeypatch):
    from looper.db import llm_records
    from tests.db_support import run_db

    async def scenario(db):
        await db.execute("INSERT INTO projects(id,name,work_dir) VALUES ('project','test',$1)", str(tmp_path))
        await db.execute("INSERT INTO tasks(id,requirement,project_id) VALUES ('task','test','project')")
        await db.execute("INSERT INTO task_steps(id,task_id,step_index,instruction) VALUES ('step','task',0,'test')")
        first = await step_attempts.open_attempt("task", "step", "step", 1, "first", pool=db)
        record = await llm_records.start_record(agent_id="worker-old", task_id="task", pool=db)
        await step_attempts.worker_started(first, record, None, pool=db)
        await llm_records.end_record_interrupted(record, "not_adopted", pool=db)
        previous = await step_attempts.previous_worker_run(first, "task", "step", pool=db)
        assert previous["status"] == "interrupted" and previous["error"] == "not_adopted"
        second = await step_attempts.open_attempt("task", "step", "step", 1, "retry", pool=db)
        assert await step_attempts.previous_worker_run(second, "task", "step", pool=db) is None
        assert await step_attempts.previous_worker_run(first, "other-task", "step", pool=db) is None
        assert await step_attempts.previous_worker_run(first, "task", "other-step", pool=db) is None
        later = await llm_records.start_record(agent_id="worker-new", task_id="task", pool=db)
        await step_attempts.worker_started(first, later, None, pool=db)
        await llm_records.finish_record(later, status="failed", pool=db)
        assert (await step_attempts.previous_worker_run(first, "task", "step", pool=db))["status"] == "failed"

    run_db(scenario, monkeypatch)


def test_fake_interrupted_worker_prompt_eval(tmp_path, monkeypatch):
    from evals.prompt_regression.worker_interrupted import run_case
    from tests.db_support import run_db

    async def scenario(db):
        result = await run_case(tmp_path, db)
        assert result["outcome"] == "pass", result

    run_db(scenario, monkeypatch)


@pytest.mark.parametrize("action", ["get", "update"])
@pytest.mark.usefixtures("clients")
def test_interruption_hint_control_is_operator_only(tmp_path, monkeypatch, action):
    from looper.agents import api_v1
    from looper.agents.ops.clients import ClientOps

    operation = api_v1.find("POST", f"/api/v1/worker_interrupted_hint/{action}")
    callback = AsyncMock(return_value={"enabled": True})
    monkeypatch.setattr(api_v1.domain_for(operation), "run", callback)

    async def scenario():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, f"POST {operation.path}", _bearer(CLIENT_TOKEN),
                json.dumps({"enabled": True} if action == "update" else {}))
            assert status == 403 and answer["error"]["code"] == "forbidden"
            callback.assert_not_awaited()

    asyncio.run(scenario())


def test_interruption_hint_setting_roundtrip(monkeypatch):
    from looper.agents import worker_interruption
    from looper.db import system_settings

    stored = {}

    async def read(keys):
        return stored.copy()

    async def write(values):
        stored.update(values)

    monkeypatch.setattr(system_settings, "get_settings", read)
    monkeypatch.setattr(system_settings, "put_settings", write)

    async def scenario():
        assert await worker_interruption.view() == {"enabled": False}
        for value in (True, False, None):
            assert await worker_interruption.update(value) == {"enabled": value is True}
        before = stored.copy()
        with pytest.raises(ValueError):
            await worker_interruption.update("true")
        assert stored == before

    asyncio.run(scenario())


@pytest.mark.parametrize("draining,enabled,expected", [(True, True, "deploy drain"), (False, True, "run stopped"),
                                                       (True, False, None)])
def test_cancelled_worker_records_only_a_typed_reason(tmp_path, monkeypatch, agent_db, draining, enabled, expected):
    from looper.agents.drain import DrainState, DrainScope

    agent_db.get_settings.return_value = {KEY: str(enabled).lower()}

    async def scenario():
        run = ScriptedRun(_events(), hold=True)
        drain = DrainState()
        async with _pool(tmp_path, monkeypatch, run, drain=drain) as (pool, _, _):
            await pool._dispatch(_item(tmp_path))
            await run.started.wait()
            if draining:
                drain.begin(DrainScope.RUNS)
            pool._tasks["w1"].cancel()
            await asyncio.gather(pool._tasks["w1"], return_exceptions=True)
            reasons = [call.args[1]["interruption_reason"] for call in agent_db.add_metadata.await_args_list
                       if "interruption_reason" in call.args[1]]
            assert reasons == ([] if expected is None else [expected])
            assert agent_db.finish_record.await_args.kwargs["status"] == "interrupted"

    asyncio.run(scenario())
