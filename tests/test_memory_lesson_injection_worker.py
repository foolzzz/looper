"""Lessons reach every work prompt and references survive outcome normalization."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from looper.agents import system_prompt
from looper.agents.task_roles import RoleKind, TaskRole
from looper.agents.tiers import worker
from looper.agents.runtimes import ADAPTERS
from looper.agents.runtimes.claude_code import ClaudeCodeRuntime
from tests.run_handle_support import ScriptedRun
from tests.worker_preparation_support import _configured_pool, _item


LESSONS = "## Lessons from earlier tasks in this project\n- M-0001 (11-01): Run the build first — it produces inputs"


def test_lessons_follow_vault_and_precede_contract_for_both_prompt_builders():
    from tests.runtime_model_helpers import builtin_launch
    settings = builtin_launch().settings
    common = dict(harness=settings.harness, contract_name="WORK_CONTRACT",
                  contract=system_prompt.work_contract(ClaudeCodeRuntime(), lessons=LESSONS),
                  task="## Task\nWork", task_kind="work", room="## Room\nMembers",
                  vault="## Vault\nRead", lessons=LESSONS)
    builtin = system_prompt.build_system_prompt(role_id="worker", role="Role", recommended=True, **common)
    role = TaskRole("specialist", RoleKind.WORK, 1, "Role")
    defined = system_prompt.build_role_prompt(boundaries=settings.boundaries, task_role=role, **common)
    for prompt in (builtin, defined):
        assert prompt.text.index("## Vault") < prompt.text.index(LESSONS) < prompt.text.index("## Your step")
        assert prompt.layers["lessons"] == {"sha256": hashlib.sha256(LESSONS.encode()).hexdigest(),
                                            "count": 1, "chars": len(LESSONS)}
        assert "lessons_used (optional): ids of the lessons above you relied on." in prompt.text
        assert "lessons_wrong (optional): ids of the lessons above that proved wrong here." in prompt.text


@pytest.mark.parametrize("verify,severity,status", [(False, False, "completed"), (False, False, "failed"),
                                                   (True, False, "completed"), (True, True, "completed")])
def test_built_in_task_role_and_verify_keep_refs(tmp_path, verify, severity, status):
    report = ({"passed": False, "findings": ["check failed"]} if verify else
              {"status": status, "changed_files": {}, "deleted_files": []})
    report.update(summary="Checked", lessons_used=["M-0001", "M-0001", "M-0003"], lessons_wrong=["M-0002"])
    event = {"subtype": "success", "result": json.dumps(report)}
    outcome = worker.judge_result(event, work_dir=tmp_path, is_verify=verify, severity=severity,
                                  lesson_ids=(1, 2))
    assert outcome["lessons_used"] == ["M-0001"]
    assert outcome["lessons_wrong"] == ["M-0002"]
    if verify:
        assert outcome["passed"] is False
    else:
        assert outcome["status"] == status


@pytest.mark.parametrize("used,wrong", [("M-0001", []), ([1], []), ([], {}), (["M-0001"], ["M-0001"])])
def test_bad_or_conflicting_references_do_not_fail_the_step(tmp_path, used, wrong):
    report = {"status": "completed", "changed_files": {}, "lessons_used": used, "lessons_wrong": wrong}
    outcome = worker.judge_result({"subtype": "success", "result": json.dumps(report)},
                                  work_dir=tmp_path, is_verify=False, lesson_ids=(1,))
    assert outcome["status"] == "completed"
    assert outcome["lessons_used"] == outcome["lessons_wrong"] == []


def test_off_judgement_has_no_reference_fields(tmp_path):
    event = {"subtype": "success", "result": json.dumps({"status": "completed", "changed_files": {},
                                                          "lessons_used": ["M-0001"]})}
    assert "lessons_used" not in worker.judge_result(event, work_dir=tmp_path, is_verify=False)
    assert system_prompt.work_contract(ClaudeCodeRuntime(), lessons="") == (
        system_prompt.WORK_CONTRACT + "\n\n" + system_prompt.WAITING_CONTRACT)
    assert system_prompt.review_contract(lessons="") == system_prompt.REVIEW_CONTRACT


@pytest.mark.parametrize("task_role,review,verification", [
    (False, False, False), (True, False, False), (False, True, False), (True, True, False),
    (False, False, True), (True, False, True)])
def test_prepare_execute_records_actual_delivery_and_refs(tmp_path, monkeypatch, agent_db, task_role, review,
                                                        verification):
    from looper.agents import memory_lesson_injection as lessons
    from looper.db import memory_lesson_injection as store
    block = lessons.LessonBlock(LESSONS, hashlib.sha256(LESSONS.encode()).hexdigest(), (1,), 0, "task")
    monkeypatch.setattr(lessons, "render_for_run", AsyncMock(return_value=block))
    monkeypatch.setattr(store, "prepare_injection", AsyncMock(return_value={"id": 41, "block": block}))
    monkeypatch.setattr(store, "bind_injection_record", AsyncMock())
    monkeypatch.setattr(store, "mark_delivered", AsyncMock(return_value={"id": 41, "state": "delivered"}))
    monkeypatch.setattr(store, "apply_references", AsyncMock(return_value={}))
    report = ({"passed": True, "findings": []} if review else {"status": "failed", "changed_files": {}})
    report.update(summary="Checked", lessons_used=["M-0001"])
    run = ScriptedRun([{"type": "system", "subtype": "init", "session_id": "synthetic-lessons"},
                       {"type": "result", "subtype": "success", "result": json.dumps(report)}])

    class Runtime(ClaudeCodeRuntime):
        hosts_runs = True
        def hosted_run(self, *args, **kwargs):
            return run

    monkeypatch.setitem(ADAPTERS, "claude-code", Runtime())
    pool = _configured_pool(tmp_path, monkeypatch,
                            {"id": "project-lessons", "agent_room": "off", "memory_lessons": "on"})
    item = _item(tmp_path / "work", review=review)
    if verification:
        item.payload["verification"] = True
    if task_role:
        role = TaskRole("specialist", RoleKind.REVIEW if review else RoleKind.WORK, 1, "Role")
        item.payload["task_role"] = role.payload()
    async def scenario():
        ctx = await pool._prepare(item, is_verify=review)
        assert isinstance(ctx, worker.RunContext)
        assert ctx.lesson_block is block
        assert LESSONS in ctx.system_prompt.text
        assert ctx.lesson_injection_id == 41
        store.mark_delivered.assert_not_awaited()
        outcome = await pool._execute_once(ctx, [])
        assert outcome["lessons_used"] == ["M-0001"]
        if verification:
            assert outcome["verification"]["limitation"] == "report_only"
        store.mark_delivered.assert_awaited_once()
        assert store.mark_delivered.call_args.kwargs["record_id"] == 1
        store.apply_references.assert_awaited_once()
        assert store.apply_references.call_args.args[0] == 41
    asyncio.run(scenario())


def test_reviewer_default_has_no_lesson_contract_or_layer(tmp_path, monkeypatch, agent_db):
    from looper.db import memory_lesson_injection as store
    from looper.agents import memory_lessons, memory_lessons_model
    monkeypatch.setattr(memory_lessons, "settings_view", AsyncMock(return_value={
        "enabled": True, "settings": memory_lessons_model.parse_settings({"enabled": True}).to_json()}))
    monkeypatch.setattr(store, "render_snapshot", AsyncMock())
    monkeypatch.setattr(store, "prepare_injection", AsyncMock())
    pool = _configured_pool(tmp_path, monkeypatch,
                            {"id": "project-lessons", "agent_room": "off", "memory_lessons": "on"})
    async def scenario():
        ctx = await pool._prepare(_item(tmp_path / "work", review=True), is_verify=True)
        assert isinstance(ctx, worker.RunContext)
        assert "lessons" not in ctx.system_prompt.layers
        assert "lessons_used" not in ctx.system_prompt.text
        store.prepare_injection.assert_not_awaited()
        store.render_snapshot.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["launch_refused", "input_unknown"])
def test_unaccepted_input_does_not_count_delivery(tmp_path, monkeypatch, agent_db, ending):
    from looper.agents import memory_lesson_injection as lessons
    from looper.db import memory_lesson_injection as store
    from looper.agents.runtimes.channel import RunStartError
    block = lessons.LessonBlock(LESSONS, hashlib.sha256(LESSONS.encode()).hexdigest(), (1,), 0, "task")
    monkeypatch.setattr(lessons, "render_for_run", AsyncMock(return_value=block))
    monkeypatch.setattr(store, "prepare_injection", AsyncMock(return_value={"id": 41, "block": block}))
    monkeypatch.setattr(store, "bind_injection_record", AsyncMock())
    for name in ("mark_delivered", "abandon_injection", "mark_uncertain", "apply_references"):
        monkeypatch.setattr(store, name, AsyncMock())
    run = ScriptedRun([], launch_error=RunStartError("refused") if ending == "launch_refused" else None,
                      start_error=BrokenPipeError("unknown") if ending == "input_unknown" else None)
    class Runtime(ClaudeCodeRuntime):
        hosts_runs = True
        def hosted_run(self, *args, **kwargs):
            return run
    monkeypatch.setitem(ADAPTERS, "claude-code", Runtime())
    pool = _configured_pool(tmp_path, monkeypatch,
                            {"id": "project-lessons", "agent_room": "off", "memory_lessons": "on"})
    async def scenario():
        ctx = await pool._prepare(_item(tmp_path / "work"))
        assert isinstance(ctx, worker.RunContext)
        outcome = await pool._execute_once(ctx, [])
        assert outcome["status"] == "failed"
        store.mark_delivered.assert_not_awaited()
        store.apply_references.assert_not_awaited()
        if ending == "launch_refused":
            store.abandon_injection.assert_awaited_once_with(41)
        else:
            store.mark_uncertain.assert_awaited_once_with(41)
    asyncio.run(scenario())


@pytest.mark.parametrize("removed_all", [False, True])
def test_prepare_uses_the_block_rechecked_at_the_delivery_boundary(tmp_path, monkeypatch, agent_db, removed_all):
    from looper.agents import memory_lesson_injection as lessons
    from looper.db import memory_lesson_injection as store
    stale_text = LESSONS + "\n- M-0002 (11-01): Old lesson — now revoked"
    stale = lessons.LessonBlock(stale_text, hashlib.sha256(stale_text.encode()).hexdigest(), (1, 2), 0, "task")
    current = lessons.LessonBlock(LESSONS, hashlib.sha256(LESSONS.encode()).hexdigest(), (1,), 0, "task")
    monkeypatch.setattr(lessons, "render_for_run", AsyncMock(return_value=stale))
    monkeypatch.setattr(store, "prepare_injection", AsyncMock(
        return_value=None if removed_all else {"id": 41, "block": current}))
    pool = _configured_pool(tmp_path, monkeypatch,
                            {"id": "project-lessons", "agent_room": "off", "memory_lessons": "on"})
    async def scenario():
        ctx = await pool._prepare(_item(tmp_path / "work"))
        assert isinstance(ctx, worker.RunContext)
        assert "M-0002" not in ctx.system_prompt.text
        assert ("lessons" in ctx.system_prompt.layers) is not removed_all
        assert ("lessons_used (optional)" in ctx.system_prompt.text) is not removed_all
        assert ctx.lesson_block == (lessons.EMPTY_BLOCK if removed_all else current)
    asyncio.run(scenario())


@pytest.mark.parametrize("stored,project_mode,empty", [
    (None, "on", False), ('{"enabled":false}', "on", False), ('broken', "on", False),
    ('{"enabled":true}', None, False), ('{"enabled":true}', "off", False),
    ('{"enabled":true}', "on", True)])
def test_disabled_prepare_matches_the_fixed_revision(tmp_path, monkeypatch, agent_db, stored, project_mode, empty):
    import ast
    import subprocess
    from looper.agents import memory_lesson_injection as lessons
    from looper.db import projects, system_settings, memory_lesson_injection as store
    project = {"id": "project-off", "agent_room": "off", "memory_lessons": project_mode}
    pool = _configured_pool(tmp_path, monkeypatch, project)
    monkeypatch.setattr(system_settings, "get_settings", AsyncMock(
        return_value={} if stored is None else {"memory_lessons": stored}))
    monkeypatch.setattr(projects, "get_project", AsyncMock(return_value=project))
    monkeypatch.setattr(store, "render_snapshot", AsyncMock(return_value=lessons.EMPTY_BLOCK))
    monkeypatch.setattr(store, "prepare_injection", AsyncMock())
    source = subprocess.run(["git", "show", "ad62143e7fc02afa840cd6771a730836d72fed89:looper/agents/tiers/worker.py"],
                            capture_output=True, text=True, check=True).stdout
    definition = next(node for node in ast.parse(source).body
                      if isinstance(node, ast.ClassDef) and node.name == "WorkerPool")
    method = next(node for node in definition.body
                  if isinstance(node, ast.AsyncFunctionDef) and node.name == "_prepare")
    namespace = dict(vars(worker))
    exec(compile(ast.Module(body=[method], type_ignores=[]), "fixed-worker-prepare", "exec"), namespace)
    async def scenario():
        item = _item(tmp_path / "work")
        current = await pool._prepare(item)
        baseline = await namespace["_prepare"](pool, item)
        assert isinstance(current, worker.RunContext) and isinstance(baseline, worker.RunContext)
        assert current.system_prompt.sha256 == baseline.system_prompt.sha256
        assert current.system_prompt.layers == baseline.system_prompt.layers
        assert current.prompt == baseline.prompt
        assert current.command == baseline.command
        assert current.launch.record() == baseline.launch.record()
        assert "lessons" not in current.system_prompt.layers
        assert "lessons_used" not in current.system_prompt.text
        assert "lessons_wrong" not in current.system_prompt.text
        store.prepare_injection.assert_not_awaited()
        assert store.render_snapshot.await_count == int(empty)
    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["refused", "held", "cancelled", "error"])
def test_admission_exit_abandons_the_prepared_delivery(tmp_path, monkeypatch, agent_db, ending):
    from contextlib import asynccontextmanager
    from looper.agents import memory_lesson_injection as lessons
    from looper.db import memory_lesson_injection as store
    block = lessons.LessonBlock(LESSONS, hashlib.sha256(LESSONS.encode()).hexdigest(), (1,), 0, "task")
    monkeypatch.setattr(lessons, "render_for_run", AsyncMock(return_value=block))
    monkeypatch.setattr(store, "prepare_injection", AsyncMock(return_value={"id": 41, "block": block}))
    monkeypatch.setattr(store, "abandon_injection", AsyncMock())
    monkeypatch.setattr(store, "mark_delivered", AsyncMock())
    run = ScriptedRun([])
    class Runtime(ClaudeCodeRuntime):
        hosts_runs = True
        def hosted_run(self, *args, **kwargs):
            return run
    monkeypatch.setitem(ADAPTERS, "claude-code", Runtime())
    failure = (worker.cost_guard.Denied("synthetic refusal") if ending == "refused" else
               worker.cost_guard.Held(datetime.now(timezone.utc) + timedelta(minutes=5), "rejected")
               if ending == "held" else
               asyncio.CancelledError() if ending == "cancelled" else RuntimeError("admission unavailable"))
    @asynccontextmanager
    async def slot(*args, **kwargs):
        raise failure
        yield
    monkeypatch.setattr(worker.admission, "slot", slot)
    pool = _configured_pool(tmp_path, monkeypatch,
                            {"id": "project-lessons", "agent_room": "off", "memory_lessons": "on"})
    async def scenario():
        with pytest.raises(type(failure)):
            await pool._execute(_item(tmp_path / "work"))
        store.prepare_injection.assert_awaited_once()
        store.abandon_injection.assert_awaited_once_with(41)
        store.mark_delivered.assert_not_awaited()
        assert run.launch_calls == 0
        assert not run.started.is_set()
    asyncio.run(scenario())
