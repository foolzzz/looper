"""An ended task proposes memories when every switch is on, and keeps the vault closeout otherwise."""

from __future__ import annotations

import asyncio
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import memory_closeout, memory_model as model, memory_settings, vault
from looper.agents.claude_cli import TurnBlocked
from looper.agents.memory_write import MemoryWriteService
from looper.agents.tiers import planner
from looper.agents.tiers.planner_protocol import TurnKind
from looper.db import memory as db_memory, system_settings
from tests import memory_support as sup
from tests.db_support import DSN, run_db
from tests.test_memory_closeout import (
    BASELINE, RULES, _sha, export_entry, off_config, off_digests, off_plan, off_row,
)

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")

LESSON_TARGETS = {
    "lesson": {"shape": "note", "folders": ["lessons"], "index": "lessons/_index.md"},
    "project": {"shape": "note", "folders": ["projects"], "index": "projects/_index.md"},
}
PREFERENCE_TARGET = {"preference": {"shape": "note", "folders": ["prefs"], "index": "prefs/_index.md"}}
LESSON_NOTE = {"op": "create_note", "path": "mem/lessons/build.md",
               "content": "---\nkind: memory\n---\nThe build needs make gen.\n"}
LESSON_INDEX = {"op": "append_line", "path": "mem/lessons/_index.md", "under": "## Index",
                "line": "- [[build]] make gen"}


def _fence(payload: dict) -> str:
    return "```json\n" + json.dumps(payload) + "\n```"


def _body(targets: dict, *, enabled: bool = True, tasks: bool = True) -> dict:
    return {"enabled": enabled, "root": "mem", "rules_files": ["mem/AGENTS.md"], "targets": targets,
            "required_frontmatter": ["kind"], "sources": {"manager": False, "tasks": tasks, "leads": False},
            "push_channel": None}


def _commit_memory_folders(repo) -> None:
    for folder in ("lessons", "projects"):
        path = repo / "mem" / folder
        path.mkdir(parents=True, exist_ok=True)
        (path / "_index.md").write_text("## Index\n")
    for args in (("add", "--", "mem"), ("-c", "commit.gpgsign=false", "commit", "-q", "-m", "Synthetic memory folders")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


async def _enable(pool, tmp_path, targets, *, enabled: bool = True, tasks: bool = True):
    repo = sup.synthetic_vault(tmp_path)
    if "lesson" in targets or "project" in targets:
        _commit_memory_folders(repo)
    await vault.update_settings({"path": str(repo), "writable": []})
    await memory_settings.update_settings(_body(targets, enabled=enabled, tasks=tasks), db=pool)
    return repo


def _agent(tmp_path, monkeypatch, *, task_id: str = "task-off"):
    agent = planner.PlannerAgent(task_id=task_id, runtime_root=tmp_path, ops=object())
    agent._token = "planner.test"
    agent._read_execution = AsyncMock(return_value=SimpleNamespace(vault_on=True))
    agent._activity = AsyncMock()
    agent._cap_reached = AsyncMock(return_value=None)
    agent._ask = AsyncMock(return_value="kept the report")
    monkeypatch.setattr(planner.vault, "active", AsyncMock(return_value=off_config()))
    monkeypatch.setattr(planner.vault, "rules_text", AsyncMock(return_value=(RULES, None)))
    return agent, off_plan(tmp_path)


async def _rows(pool, project_id, task_id: str = "task-off") -> list[int]:
    await pool.execute("INSERT INTO tasks (id, requirement, project_id) VALUES ($1, 'Build a report endpoint.', $2)",
                       task_id, project_id)
    ids = []
    for step_id, summary in (
            ("s1", "the report endpoint needs the existing client"),
            ("s2", "the second accepted step names its client")):
        ids.append(await pool.fetchval(
            "INSERT INTO task_journal (task_id, step_id, kind, summary) "
            "VALUES ($1, $2, 'step_accepted', $3) RETURNING id",
            task_id, step_id, summary))
    return ids


def _proposals() -> str:
    return _fence({"memory": [
        {"kind": "lesson", "statement": "The report endpoint needs the existing client.",
         "why": "a new client would drift from the one the service already calls",
         "keywords": ["report"], "evidence": ["J1"]},
        {"kind": "project", "statement": "The report service lives under the existing client.",
         "why": "that directory is where the service already runs",
         "keywords": ["service"], "evidence": ["J2"]},
    ], "note": "proposed the client fact and the service path"})


def test_memory_closeout_replaces_vault_closeout_when_all_switches_are_on(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects_on(pool, tmp_path)
        journal_ids = await _rows(pool, project["id"])
        agent, plan = _agent(tmp_path, monkeypatch)
        plan.project_id = project["id"]
        asked = []

        async def ask(kind, message, parse):
            asked.append((kind, message))
            return parse(_proposals())

        agent._ask = ask
        await agent._vault_closeout(plan, "completed")
        assert [kind for kind, _ in asked] == [TurnKind.MEMORY]
        assert "the report endpoint needs the existing client" in asked[0][1]
        assert "[VAULT]" not in asked[0][1]
        rows = await pool.fetch("SELECT * FROM memory_candidates ORDER BY id")
        assert len(rows) == 2
        assert {row["kind"] for row in rows} == {"lesson", "project"}
        assert all(row["state"] == "drafting" and row["source_kind"] == "task" and row["source_class"] == "agent"
                   and row["project_id"] == project["id"] for row in rows)
        sources = await pool.fetch("SELECT candidate_id, journal_id FROM memory_candidate_sources ORDER BY journal_id")
        assert [row["journal_id"] for row in sources] == journal_ids
        events = await pool.fetch("SELECT action, actor_kind FROM memory_candidate_events ORDER BY id")
        assert [(row["action"], row["actor_kind"]) for row in events] == [("propose", "planner"), ("propose", "planner")]
        kind, summary = agent._activity.await_args.args
        proposed = ", ".join(model.format_id(row["id"]) for row in rows)
        assert kind == "phase"
        assert summary == f"Memory closeout: proposed {proposed}: proposed the client fact and the service path"

    run_db(run, monkeypatch)


async def projects_on(pool, tmp_path, *, mode: str | None = "on"):
    from looper.db import projects

    project = await projects.create_project(name="Reports", pool=pool)
    if mode == "on":
        await projects.set_memory_closeout(project["id"], "on", pool=pool)
    await _enable(pool, tmp_path, LESSON_TARGETS)
    return project


@pytest.mark.parametrize("case", ["no_row", "disabled", "stale", "tasks_off", "column_null", "column_off",
                                   "preference_only"])
def test_vault_closeout_runs_when_any_switch_is_off(tmp_path, monkeypatch, case):
    async def run(pool):
        from looper.db import projects

        project = await projects.create_project(name="Reports", pool=pool)
        if case not in ("no_row", "column_null"):
            if case == "column_off":
                await pool.execute("UPDATE projects SET memory_closeout = 'off' WHERE id = $1", project["id"])
            else:
                await projects.set_memory_closeout(project["id"], "on", pool=pool)
        if case == "stale":
            await system_settings.put_settings({"memory_write": json.dumps(
                {"enabled": True, "root": "mem", "sources": {"tasks": True}})}, pool=pool)
        elif case == "disabled":
            await _enable(pool, tmp_path, LESSON_TARGETS, enabled=False)
        elif case == "tasks_off":
            await _enable(pool, tmp_path, LESSON_TARGETS, tasks=False)
        elif case == "preference_only":
            await _enable(pool, tmp_path, PREFERENCE_TARGET)
        elif case in ("column_null", "column_off"):
            await _enable(pool, tmp_path, LESSON_TARGETS)
        agent, plan = _agent(tmp_path, monkeypatch)
        plan.project_id = project["id"]
        await agent._vault_closeout(plan, "completed")
        agent._ask.assert_awaited_once()
        assert agent._ask.await_args.args[0] is TurnKind.VAULT
        assert _sha(agent._ask.await_args.args[1]) == BASELINE["sha256"]["vault_message_completed"]
        failed, failed_plan = _agent(tmp_path, monkeypatch)
        failed_plan.project_id = project["id"]
        await failed._vault_closeout(failed_plan, "failed: the report build broke")
        assert _sha(failed._ask.await_args.args[1]) == BASELINE["sha256"]["vault_message_failed"]
        assert off_digests(tmp_path) == BASELINE["sha256"]

    run_db(run, monkeypatch)


def test_empty_journal_and_spent_cap_skip_both_closeouts(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects_on(pool, tmp_path)
        agent, plan = _agent(tmp_path, monkeypatch)
        plan.project_id = project["id"]
        plan.journal.clear()
        await agent._vault_closeout(plan, "completed")
        agent._ask.assert_not_awaited()
        agent._activity.assert_awaited_once_with("phase", "No memory closeout: the task journal is empty")

        await _rows(pool, project["id"], task_id="task-capped")
        capped, capped_plan = _agent(tmp_path, monkeypatch, task_id="task-capped")
        capped._cap_reached.return_value = 2.5
        capped_plan.project_id = project["id"]
        await capped._vault_closeout(capped_plan, "completed")
        capped._ask.assert_not_awaited()
        capped._activity.assert_awaited_once_with(
            "error", "Warning: no memory closeout: the task has spent its $2.5 cap (task_budget_usd)")

    run_db(run, monkeypatch)


def test_once_per_planner_and_failures_never_fail_the_task(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects_on(pool, tmp_path)
        await _rows(pool, project["id"])
        agent, plan = _agent(tmp_path, monkeypatch)
        plan.project_id = project["id"]
        plan.status = "completed"

        async def ask(kind, message, parse):
            return parse(_proposals())

        agent._ask = ask
        await agent._vault_closeout(plan, "completed")
        await agent._vault_closeout(plan, "completed")
        assert agent._activity.await_count == 1
        assert plan.status == "completed"

        warned, warned_plan = _agent(tmp_path, monkeypatch, task_id="task-off")
        warned_plan.project_id = project["id"]
        warned_plan.status = "completed"
        warned._ask.side_effect = planner.PlannerTurnError("the memory turn failed")
        await warned._vault_closeout(warned_plan, "completed")
        assert warned._activity.await_args.args[0] == "error"
        assert warned._activity.await_args.args[1].startswith("Warning: the Planner's memory closeout failed:")
        assert warned_plan.status == "completed"

        async def explode(*_args, **_kwargs):
            raise RuntimeError("the candidate store refused the write")

        monkeypatch.setattr(db_memory, "create", explode)
        broken, broken_plan = _agent(tmp_path, monkeypatch, task_id="task-broken")
        await _rows(pool, project["id"], task_id="task-broken")
        broken_plan.project_id = project["id"]
        broken_plan.status = "completed"

        async def ask_again(kind, message, parse):
            return parse(_proposals())

        broken._ask = ask_again
        await broken._vault_closeout(broken_plan, "completed")
        assert broken._activity.await_args.args[0] == "error"
        assert "the candidate store refused the write" in broken._activity.await_args.args[1]
        assert broken_plan.status == "completed"

        blocked, blocked_plan = _agent(tmp_path, monkeypatch, task_id="task-blocked")
        await _rows(pool, project["id"], task_id="task-blocked")
        blocked_plan.project_id = project["id"]
        blocked._ask.side_effect = TurnBlocked("Request Blocked by Security Policy", str(tmp_path))
        with pytest.raises(TurnBlocked):
            await blocked._vault_closeout(blocked_plan, "completed")

    run_db(run, monkeypatch)


def test_tombstoned_seed_is_dropped_and_counted(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects_on(pool, tmp_path)
        journal_ids = await _rows(pool, project["id"])
        statement = "The report endpoint needs the existing client."
        async with db_memory.transaction(pool) as conn:
            created = await db_memory.create(
                conn, kind="lesson", statement=statement, why="a new client would drift",
                keywords=["report"], evidence=[db_memory.Evidence(model.EvidenceKind.JOURNAL, journal_id=journal_ids[0])],
                actor=model.Actor.PLANNER, via=model.Via.SYSTEM, project_id=project["id"])
            await db_memory.delete(conn, created, via=model.Via.DASHBOARD)
        agent, plan = _agent(tmp_path, monkeypatch)
        plan.project_id = project["id"]

        async def ask(kind, message, parse):
            return parse(_fence({"memory": [
                {"kind": "lesson", "statement": statement, "why": "a new client would drift",
                 "keywords": ["report"], "evidence": ["J1"]},
            ], "note": "the client fact was already deleted"}))

        agent._ask = ask
        await agent._vault_closeout(plan, "completed")
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates") == 0
        assert agent._activity.await_args.args == (
            "phase", "Memory closeout: the client fact was already deleted; dropped 1 (tombstoned: 1)")

    run_db(run, monkeypatch)


def test_failed_task_closeout_says_failed(tmp_path, monkeypatch):
    async def run(pool):
        project = await projects_on(pool, tmp_path)
        await _rows(pool, project["id"])
        seen = []
        agent, plan = _agent(tmp_path, monkeypatch)
        plan.project_id = project["id"]
        agent._bus = SimpleNamespace(publish=AsyncMock())
        agent._save = AsyncMock()
        monkeypatch.setattr(planner, "update_task_phase", AsyncMock())

        async def ask(kind, message, parse):
            seen.append(message)
            return parse(_fence({"memory": [], "note": "nothing survived the failure"}))

        agent._ask = ask
        await agent._fail_plan(plan, reason="the report build broke")
        assert plan.status == "failed" and seen[0].startswith("[MEMORY] The task failed: the report build broke")
        assert any(call.args == ("phase", "Memory closeout: nothing survived the failure")
                   for call in agent._activity.await_args_list)

        quiet, quiet_plan = _agent(tmp_path, monkeypatch, task_id="task-quiet")
        quiet_plan.project_id = project["id"]
        quiet._bus = SimpleNamespace(publish=AsyncMock())
        quiet._save = AsyncMock()
        await quiet._fail_plan(quiet_plan, reason="the report build broke", model_closeout=False)
        quiet._ask.assert_not_awaited()

    run_db(run, monkeypatch)


def test_written_task_candidate_adds_an_activity_line_to_its_task(tmp_path, monkeypatch):
    async def run(pool):
        repo = await _enable(pool, tmp_path, {**PREFERENCE_TARGET, **LESSON_TARGETS})
        journal_id = await sup.journal_entry(pool)
        lesson = await sup.candidate(
            pool, kind="lesson", statement="The build needs make gen",
            evidence=[db_memory.Evidence(model.EvidenceKind.JOURNAL, journal_id=journal_id)],
            actor=model.Actor.PLANNER, via=model.Via.SYSTEM, project_id="proj-mem")
        preference = await sup.candidate(pool, statement="Weekly reports: conclusion first")
        settings, problem = await memory_settings.load(pool)
        assert problem is None
        for candidate_id, changes, title, note_path in (
                (lesson, [LESSON_NOTE, LESSON_INDEX], "The build needs make gen", LESSON_NOTE["path"]),
                (preference, [sup.NOTE, sup.INDEX], "Weekly reports: conclusion first", sup.NOTE["path"])):
            revised = await sup.revise_current(pool, candidate_id, settings, changes=changes,
                                               note_path=note_path, title=title)
            await sup.move(pool, candidate_id, "confirmed", "confirm", actor="user", via="dashboard",
                           content_hash=revised["content_hash"])
        service = MemoryWriteService(tmp_path / "runtime", db=pool)
        assert await service.run_once()
        # The lesson commit is the one the activity names; the preference write moves HEAD afterwards.
        commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                check=True, capture_output=True, text=True).stdout.strip()
        assert await service.run_once()
        lines = await pool.fetch(
            "SELECT task_id, worker, kind, summary FROM activity WHERE worker = 'memory' ORDER BY seq")
        assert len(lines) == 1
        assert lines[0]["task_id"] == "task-mem" and lines[0]["kind"] == "phase"
        assert lines[0]["summary"] == (
            f"Memory: {model.format_id(lesson)} written to {LESSON_NOTE['path']} (commit {commit[:12]})")
        assert (await db_memory.get(preference, db=pool))["candidate"]["state"] == "written"

    run_db(run, monkeypatch, timeout=60)
