"""Default-off byte compatibility with the main revision recorded in the fixture.

The provenance case regenerates the fixture from that revision; ordinary cases
need neither git history nor an installed model CLI.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from looper.agents import metrics_alerts, system_prompt
from looper.agents.agent_settings import AgentSettings, Harness, Layer
from looper.agents.knowledge import EMPTY_KNOWLEDGE
from looper.agents.project_execution import ProjectExecution
from looper.agents.tiers import planner, planner_protocol, worker
from looper.db import plans, projects, tasks
from tests.db_support import DSN, run_db

FIXTURE = Path(__file__).parent / "fixtures" / "acceptance_checks_off.json"
BASELINE = json.loads(FIXTURE.read_text()) if FIXTURE.exists() else {}
STARTED_AT = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def _step(module, status="pending"):
    return module.PlanStep(
        step_id="step-off", key="s1", index=0, instruction="Implement the report endpoint.",
        acceptance_criteria=["A valid report returns 200.", "Missing reports return 404."],
        target_files=["report.py"], validation_commands=["python -m pytest tests/test_report.py"],
        review=True, status=status, result={"status": "completed", "summary": "Implemented report lookup."},
        worker_id="worker-off", retries=1, feedback=["Keep the missing report case."],
        started_at=None if status == "pending" else STARTED_AT,
    )


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=lambda item: item.isoformat())


def _outputs(modules, runtime_root, execution=None):
    p, protocol, prompt, db_tasks = modules.planner, modules.protocol, modules.prompt, modules.tasks
    w = modules.worker
    agent = p.PlannerAgent(task_id="task-off", runtime_root=runtime_root)
    if execution is not None:
        agent._execution = execution
    step = _step(p)
    task = p.TaskPlan("plan-off", "goal-off", "Build a report endpoint.", "/workspace/report", steps=[step],
                      project_id="project-off", project_name="Reports", project_background="Use the existing API.")
    settings = p.PlannerSettings("Plan carefully and preserve the API.", 60, 0, 60)
    outputs = {
        "planner_contract": protocol.reply_contract(),
        "planner_system_prompt": agent._system_prompt(task, settings, EMPTY_KNOWLEDGE,
                                                       AgentSettings().harness).text,
        "planner_plan_turn": agent._plan_message(task),
        "planner_step_row": _json(p._step_row(step)),
    }
    for result in (protocol.ResultKind.WORK, protocol.ResultKind.REVIEW):
        for succeeded in (False, True):
            context = protocol.DecisionContext("s1", result, succeeded, True, 1, last_open=True)
            outputs[f"planner_decision_{result.value}_{succeeded}"] = agent._decision_message(
                task, step, context, ["Synthetic result: report lookup was exercised."])
    payload = {"requirement": task.requirement, "step_key": "s1", "instruction": step.instruction,
               "acceptance_criteria": step.acceptance_criteria, "validation_commands": step.validation_commands,
               "target_files": step.target_files, "feedback": step.feedback, "attempt": 2,
               "work_result": {"summary": "Report lookup implemented.", "changed_files": {"report.py": "Lookup"}}}
    for role in ("worker", "reviewer"):
        verify = role == "reviewer"
        task_section = w.WorkerPool._task_section(payload, task.work_dir, is_verify=verify)
        outputs[f"{role}_first_turn"] = (w.WorkerPool._build_verify_prompt(None, payload) if verify
                                         else w.WorkerPool._build_work_prompt(None, payload))
        outputs[f"{role}_system_prompt"] = prompt.build_system_prompt(
            harness=Harness("Verify each change.", Layer.BUILTIN), role_id=role,
            role=w.REVIEWER_SYSTEM_PROMPT if verify else w.DEFAULT_SYSTEM_PROMPT, recommended=True,
            task_kind="review" if verify else "work", task_id="task-off", step_id="step-off",
            task=task_section, contract_name="REVIEW_CONTRACT" if verify else "WORK_CONTRACT",
            contract=prompt.REVIEW_CONTRACT if verify else prompt.WORK_CONTRACT,
        ).text
    row = {**p._step_row(step), "task_id": "task-off", "result": _json(step.result), "review_roles": "[]"}
    outputs["public_step_row"] = _json(db_tasks.step_row(row))
    return outputs


def _hashes(outputs):
    return {name: hashlib.sha256(value.encode("utf-8")).hexdigest() for name, value in outputs.items()}


def _main_modules(revision):
    """Load only the pinned modules, under aliases so the checkout stays intact."""
    found = {}
    for key, name in (("protocol", "looper.agents.tiers.planner_protocol"),
                      ("prompt", "looper.agents.system_prompt"), ("planner", "looper.agents.tiers.planner"),
                      ("tasks", "looper.db.tasks"), ("plans", "looper.db.plans"),
                      ("worker", "looper.agents.tiers.worker")):
        source = subprocess.check_output(["git", "show", f"{revision}:{name.replace('.', '/')}.py"], text=True)
        alias = name.rsplit(".", 1)[0] + "._acceptance_baseline_" + key
        module = ModuleType(alias)
        module.__file__ = name.replace(".", "/") + ".py"
        module.__package__ = name.rsplit(".", 1)[0]
        sys.modules[alias] = module
        exec(compile(source, module.__file__, "exec"), module.__dict__)
        found[key] = module
    for name in ("reply_contract", "Decision", "ResultKind"):
        setattr(found["planner"], name, getattr(found["protocol"], name))
    for name in ("build_system_prompt", "plan_task_section"):
        setattr(found["planner"], name, getattr(found["prompt"], name))
    for name in ("work_task_section", "review_task_section"):
        setattr(found["worker"], name, getattr(found["prompt"], name))
    return SimpleNamespace(**found)


@pytest.mark.parametrize("row", [{}, {"acceptance_checks": None}, {"acceptance_checks": "off"}],
                         ids=["unset", "null", "explicit-off"])
def test_default_off_model_text_and_step_serialization_match_main_bytes(tmp_path, monkeypatch, row):
    monkeypatch.setattr(metrics_alerts, "_enabled", False)
    execution = ProjectExecution.from_row(row)
    current = SimpleNamespace(planner=planner, protocol=planner_protocol, prompt=system_prompt, tasks=tasks, worker=worker)
    assert _hashes(_outputs(current, tmp_path, execution)) == BASELINE["sha256"]
    # Exercise the explicit argument as well as the default used by legacy callers.
    assert hashlib.sha256(planner_protocol.reply_contract(
        acceptance_checks=execution.acceptance_checks).encode("utf-8")).hexdigest() == BASELINE["sha256"]["planner_contract"]


@pytest.mark.provenance
def test_default_off_fixture_was_generated_from_main(tmp_path):
    baseline = _main_modules(BASELINE["main_revision"])
    assert _hashes(_outputs(baseline, tmp_path)) == BASELINE["sha256"]
    assert baseline.plans._SAVE_STEP == BASELINE["step_insert_sql"]
    for status, arguments in BASELINE["step_arguments"].items():
        assert _json(baseline.plans._step_args("task-off", baseline.planner._step_row(
            _step(baseline.planner, status)))) == _json(arguments)

    current_agent = planner.PlannerAgent(task_id="task-off", runtime_root=tmp_path)
    baseline_agent = baseline.planner.PlannerAgent(task_id="task-off", runtime_root=tmp_path)
    current_plan = planner.TaskPlan("plan-off", "goal-off", "Report", "", steps=[_step(planner)])
    baseline_plan = baseline.planner.TaskPlan("plan-off", "goal-off", "Report", "", steps=[_step(baseline.planner)])
    assert "\n".join(current_agent._plan_lines(current_plan)).encode() == (
        "\n".join(baseline_agent._plan_lines(baseline_plan)).encode())


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("status", ["pending", "dispatched", "completed", "verified"])
def test_default_off_stored_step_bytes_match_main_sql(monkeypatch, status):
    async def check(pool):
        project = await projects.create_project(
            name="Acceptance compatibility", pool=pool)
        assert project.get("acceptance_checks") is None
        await tasks.create_task(task_id="task-off", project_id=project["id"], requirement="Report lookup", pool=pool)
        old_arguments = BASELINE["step_arguments"][status].copy()
        if old_arguments[23] is not None:
            old_arguments[23] = datetime.fromisoformat(old_arguments[23])
        async with pool.acquire() as conn, conn.transaction():
            # Both writes use transaction_timestamp(), so even generated timestamps compare byte for byte.
            await conn.execute(BASELINE["step_insert_sql"], *old_arguments)
            before = await conn.fetchval("SELECT (to_jsonb(s) - 'checks')::text FROM task_steps s WHERE id='step-off'")
            old_raw = dict(await conn.fetchrow("SELECT * FROM task_steps WHERE id='step-off'"))
            old_raw.pop("checks")
            await conn.execute("DELETE FROM task_steps WHERE id='step-off'")
            step = planner._step_row(_step(planner, status))
            await conn.execute(plans._SAVE_STEP, *plans._step_args("task-off", step))
            after = await conn.fetchval("SELECT (to_jsonb(s) - 'checks')::text FROM task_steps s WHERE id='step-off'")
            assert after.encode("utf-8") == before.encode("utf-8")
            raw = await conn.fetchrow("SELECT * FROM task_steps WHERE id='step-off'")
            assert json.loads(raw["checks"]) == []
            old_public = tasks.step_row(old_raw)
            assert _json(tasks.step_row(raw)) == _json(old_public)
    run_db(check, monkeypatch)
