"""Claimed gate input survives edits and is consumed only by matching execution settings."""
from __future__ import annotations

import asyncio
import os

import pytest

from looper.agents import schedules
from looper.db import managers, role_templates, schedules as db_schedules, tasks
from tests.db_support import DSN, until
from tests.schedule_support import TELEGRAM_CHAT, allow_telegram, run_scheduled

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
PROFILE = "agent-claude-code-opus"
OWNER = "manager-gate"
GATE = "if [ -s input ]; then mv input claimed; cat claimed; touch started; read ack < release; fi"


async def setup(env, path):
    os.mkfifo(path / "release")
    (path / "input").write_text("claimed-alert\n")
    await env.pool.execute("INSERT INTO managers (id,name,system_prompt) VALUES ($1,'Gate owner','Inspect alerts.')", OWNER)
    await managers.put_template(OWNER, "report", {
        "name": "Report", "requirement": "Inspect {{source}}", "inputs": [{"key": "source", "label": "Source"}]})
    return (await env.enabled(manager_id=OWNER, gate_command=GATE, gate_timeout_seconds=10, template_key="report",
                              template_inputs={"source": "original"}))["schedule_id"]


async def release(env, path):
    # FIFO rendezvous: the command has claimed and printed input before accepting this acknowledgement.
    fd = os.open(path / "release", os.O_RDWR | os.O_NONBLOCK)
    try:
        os.write(fd, b"continue\n")
        await asyncio.wait_for(env.scheduler.idle(), 15)
    finally:
        os.close(fd)


async def fire(env, sid):
    await env.due(sid)
    await env.scheduler.tick()
    await asyncio.wait_for(env.scheduler.idle(), 15)
    return (await env.runs(sid))[0]


@pytest.mark.parametrize("edit", [False, True])
def test_presentation_edit_during_actual_gate_preserves_claim(monkeypatch, tmp_path, edit):
    async def test(env):
        sid = await setup(env, tmp_path)
        await allow_telegram(env)
        rid = await env.scheduler.run_now(sid)
        try:
            await until(lambda: (tmp_path / "started").exists())
            if edit:
                await schedules.update_schedule(sid, {"name": "Renamed", "notify": "never",
                    "notify_target": {"channel": "telegram", "chat": TELEGRAM_CHAT}})
        finally:
            await release(env, tmp_path)
        run = await db_schedules.get_run(rid)
        assert run["status"] == "running", run
        assert run["gate_output"] == "claimed-alert"
        assert "claimed-alert" in (await tasks.get_task(run["task_id"]))["requirement"]
        assert await env.pool.fetchval("SELECT count(*) FROM tasks") == 1
        saved = await db_schedules.get_schedule(sid)
        if edit:
            assert (saved["name"], saved["notify"], saved["notify_channel"], saved["notify_chat"]) == (
                "Renamed", "never", "telegram", TELEGRAM_CHAT)
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("field,changed", [("requirement", "Different work"),
    ("template_inputs", {"source": "different"}), ("template_key", "other")])
def test_execution_edit_retains_snapshot_until_restored_and_carries_once(monkeypatch, tmp_path, field, changed):
    async def test(env):
        sid = await setup(env, tmp_path)
        if field == "template_key":
            await managers.put_template(OWNER, "other", {"name": "Other", "requirement": "Inspect {{source}}",
                "inputs": [{"key": "source", "label": "Source"}]})
        original = await db_schedules.get_schedule(sid)
        inputs = {"template_inputs": original["template_inputs"]} if field == "template_key" else {}
        rid = await env.scheduler.run_now(sid)
        try:
            await until(lambda: (tmp_path / "started").exists())
            assert (await db_schedules.get_run(rid))["execution_snapshot"]["settings"][field] == original[field]
            await schedules.update_schedule(sid, {field: changed, **inputs})
        finally:
            await release(env, tmp_path)
        [failed] = await env.runs(sid)
        assert failed["status"] == "failed" and "changed" in failed["error"]
        assert failed["gate_output"] == "claimed-alert"
        snapshot = failed["execution_snapshot"]
        assert snapshot["settings"][field] == original[field]
        assert (await db_schedules.get_run(rid))["execution_snapshot"] == snapshot
        assert (await schedules.get_schedule(sid))["schedule"]["last_run"]["execution_snapshot"] == snapshot
        assert await env.pool.fetchval("SELECT count(*) FROM tasks") == 0
        # The current execution must not consume the retained input.
        assert (await fire(env, sid))["status"] == "skipped_gate"
        await schedules.update_schedule(sid, {field: original[field], **inputs})
        carrying = await fire(env, sid)
        assert (carrying["status"], carrying["carried_from"]) == ("running", rid)
        assert "claimed-alert" in (await tasks.get_task(carrying["task_id"]))["requirement"]
        assert await env.pool.fetchval("SELECT carried_on FROM schedule_runs WHERE id=$1", rid)
        await env.end_task(carrying["task_id"], "completed")
        assert (await fire(env, sid))["status"] == "skipped_gate"
        assert await env.pool.fetchval("SELECT count(*) FROM tasks") == 1
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("legacy", [False, True])
def test_restored_template_and_legacy_unfinished_input(monkeypatch, tmp_path, legacy):
    async def test(env):
        sid = await setup(env, tmp_path)
        rid = await env.scheduler.run_now(sid)
        try:
            await until(lambda: (tmp_path / "started").exists())
            await managers.put_template(OWNER, "report", {"name": "Report", "requirement": "Different work"})
        finally:
            await release(env, tmp_path)
        failed = (await env.runs(sid))[0]
        assert failed["status"] == "failed" and failed["gate_output"] == "claimed-alert"
        if legacy:
            # Pre-snapshot records keep their old carry behavior, even with different current settings.
            await db_schedules.update_run(env.pool, rid, execution_snapshot=None)
        else:
            assert (await fire(env, sid))["status"] == "skipped_gate"
            await managers.put_template(OWNER, "report", {"name": "Report", "requirement": "Inspect {{source}}"})
        carrying = await fire(env, sid)
        assert (carrying["status"], carrying["carried_from"]) == ("running", rid)
        assert "claimed-alert" in (await tasks.get_task(carrying["task_id"]))["requirement"]
        await env.end_task(carrying["task_id"], "completed")
        assert (await fire(env, sid))["status"] == "skipped_gate"
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("mutate", [False, True])
def test_final_creation_comparison_preserves_input_and_rolls_back(monkeypatch, tmp_path, mutate):
    async def test(env):
        sid = await setup(env, tmp_path)
        await env.pool.execute("UPDATE projects SET planner_roles='on' WHERE id=$1", env.project_id)
        role = {"kind": "review", "role_prompt": "Check original sources."}
        await role_templates.put_template(env.project_id, "check", role, pool=env.pool)
        await managers.put_template(OWNER, "report", {"name": "Report", "role_templates": ["check"],
            "reviewer_profile_id": PROFILE})
        await schedules.update_schedule(sid, {"task_limits": {"worker": 2}})
        entered, resume = asyncio.Event(), asyncio.Event()
        original = env.scheduler._create_task

        async def at_creation(conn, *args, **kwargs):
            assert conn.is_in_transaction()  # inside the task creation savepoint, after completion comparison
            entered.set()
            await asyncio.wait_for(resume.wait(), 10)
            if mutate:
                # Dependencies are SHARE-locked by completion. Edit on that transaction to exercise the final
                # comparison deterministically, without weakening locks or replacing the fingerprint.
                await role_templates.put_template(env.project_id, "check", {**role, "role_prompt": "Changed"}, pool=conn)
            return await original(conn, *args, **kwargs)

        monkeypatch.setattr(env.scheduler, "_create_task", at_creation)
        rid = await env.scheduler.run_now(sid)
        try:
            await until(lambda: (tmp_path / "started").exists())
            releasing = asyncio.create_task(release(env, tmp_path))
            await asyncio.wait_for(entered.wait(), 10)
            async with env.pool.acquire() as observer, observer.transaction():
                assert await observer.fetchval("SELECT count(*) FROM tasks") == 0
                resume.set()
            await releasing
        finally:
            resume.set()
            await asyncio.wait_for(env.scheduler.idle(), 15)
        run = (await schedules.list_runs(sid, {}))["runs"][0]
        assert run["run_id"] == rid and run["gate_output"] == "claimed-alert"
        if mutate:
            assert run["status"] == "failed" and "changed" in run["error"]
            assert run["execution_snapshot"]["effective"]["roles"][0]["role_prompt"] == role["role_prompt"]
        else:
            assert run["status"] == "running"
        for table in ("tasks", "task_roles", "task_profile_overrides", "task_concurrency_limits"):
            assert await env.pool.fetchval(f"SELECT count(*) FROM {table}") == (0 if mutate else 1)
        assert (await role_templates.get_template(env.project_id, "check"))["role_prompt"] == role["role_prompt"]
        if mutate:
            monkeypatch.setattr(env.scheduler, "_create_task", original)
            carrying = await fire(env, sid)
            assert (carrying["status"], carrying["carried_from"]) == ("running", rid)
            await env.end_task(carrying["task_id"], "completed")
            assert (await fire(env, sid))["status"] == "skipped_gate"
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("boundary", ["disabled", "owner"])
def test_gate_edit_does_not_bypass_stop_or_owner_boundary(monkeypatch, tmp_path, boundary):
    async def test(env):
        sid = await setup(env, tmp_path)
        rid = await env.scheduler.run_now(sid)
        try:
            await until(lambda: (tmp_path / "started").exists())
            if boundary == "disabled":
                await schedules.update_schedule(sid, {"enabled": False})
            else:
                await env.pool.execute("UPDATE managers SET enabled=false WHERE id=$1", OWNER)
        finally:
            await release(env, tmp_path)
        run = (await env.runs(sid))[0]
        assert run["status"] == "failed" and run["gate_output"] == "claimed-alert"
        assert run["execution_snapshot"] and run["error"]
        assert await env.pool.fetchval("SELECT count(*) FROM tasks") == 0
        assert not await env.pool.fetchval("SELECT carried_on FROM schedule_runs WHERE id=$1", rid)
        if boundary == "owner":
            # A retained snapshot cannot bypass the owner's current permission boundary.
            denied = await env.scheduler.run_now(sid)
            await env.scheduler.idle()
            assert (await db_schedules.get_run(denied))["status"] == "failed"
            assert await env.pool.fetchval("SELECT count(*) FROM tasks") == 0
            await env.pool.execute("UPDATE managers SET enabled=true WHERE id=$1", OWNER)
            carrying = await fire(env, sid)
            assert (carrying["status"], carrying["carried_from"]) == ("running", rid)
    run_scheduled(test, monkeypatch, tmp_path)
