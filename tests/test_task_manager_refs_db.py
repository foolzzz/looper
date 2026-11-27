"""The task branch, the ref reports of a direct turn and the branch operation (PostgreSQL)."""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from looper.agents import task_manager_refs as refs, task_manager_safety
from looper.agents.tiers import task_manager as tm
from looper.db import task_chat, tasks
from tests.db_support import run_db, v1_api
from tests.task_manager_support import NativeTaskManager, Sink, submit_and_wait


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=Tester", "-c", "user.email=tester@example.invalid",
                           "-c", "commit.gpgsign=false", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("a\n")
    git(path, "add", "--", "a.txt")
    git(path, "commit", "-qm", "initial")
    git(path, "branch", "other")
    return path


@pytest.fixture
def native(monkeypatch, tmp_path):
    return NativeTaskManager(monkeypatch, tmp_path)


async def make_task(native, db, tmp_path, task_id="task-one", **kwargs):
    directory = init_repo(tmp_path / task_id)
    await native.create(db, task_id, path=directory, **kwargs)
    return task_id, directory


def events_of(sink, kind):
    return [payload for name, payload in sink.events if name == kind]


def test_first_enablement_records_the_branch_once_and_says_so_once(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, directory = await make_task(native, db, tmp_path)
        sink = Sink()
        before, branch = await refs.begin_turn({"id": task_id}, directory, sink=sink)
        assert branch == "main" and before.head == "refs/heads/main"
        row = await db.fetchrow("SELECT branch, recorded_by FROM task_manager_branches WHERE task_id=$1", task_id)
        assert (row["branch"], row["recorded_by"]) == ("main", "first_enable")
        notice, = events_of(sink, "sandbox_notice")
        assert notice["notice"] == "first_enable_in_place" and "never restores" in notice["detail"]
        await refs.begin_turn({"id": task_id}, directory, sink=sink)
        assert len(events_of(sink, "sandbox_notice")) == 1
    run_db(check, monkeypatch, timeout=60)


def test_a_detached_head_refuses_the_first_enablement_and_records_nothing(monkeypatch, tmp_path, native):
    task_manager_safety.refusal_counts.clear()

    async def check(db):
        task_id, directory = await make_task(native, db, tmp_path)
        git(directory, "checkout", "-q", "--detach")
        sink = Sink()
        with pytest.raises(tm.Refused) as refused:
            await refs.begin_turn({"id": task_id}, directory, sink=sink)
        assert refused.value.reason is tm.RefuseReason.HEAD_DETACHED
        assert await db.fetchval("SELECT count(*) FROM task_manager_branches") == 0 and sink.events == []
        assert task_manager_safety.refusal_counts[("head_detached", "turn", "unknown")] == 1
    run_db(check, monkeypatch, timeout=60)


def test_a_moved_head_refuses_the_next_turn_until_the_branch_is_checked_out_or_set(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, directory = await make_task(native, db, tmp_path)
        sink = Sink()
        before, branch = await refs.begin_turn({"id": task_id}, directory, sink=sink)
        git(directory, "checkout", "-q", "other")
        report = await refs.end_turn({"id": task_id}, directory, before, branch, sink=sink, message_id="m1")
        assert (report.head_before, report.head_after) == ("refs/heads/main", "refs/heads/other")
        said, = events_of(sink, "ref_report")
        assert said["head"] == {"before": "refs/heads/main", "after": "refs/heads/other"} and said["report_id"] is None
        assert await db.fetchval("SELECT count(*) FROM task_manager_ref_reports") == 0  # a HEAD change alone: event only
        task_manager_safety.refusal_counts.clear()
        with pytest.raises(tm.Refused) as refused:
            await refs.begin_turn({"id": task_id}, directory, sink=sink)
        assert refused.value.reason is tm.RefuseReason.HEAD_MOVED
        text = str(refused.value)
        assert "main" in text and "other" in text and "git switch -- main" in text and "task_manager_branch_set" in text
        assert task_manager_safety.refusal_counts[("head_moved", "turn", "unknown")] == 1
        async with v1_api(tmp_path, db) as call:
            status, answer = await call("tasks.task_manager_branch_set", {"task_id": task_id})
            assert status == 200, answer
            assert answer["result"] == {"task_id": task_id, "branch": "other", "previous": "main"}
        await refs.begin_turn({"id": task_id}, directory, sink=sink)  # passes on the newly recorded branch
        git(directory, "checkout", "-q", "main")
        with pytest.raises(tm.Refused):
            await refs.begin_turn({"id": task_id}, directory, sink=sink)
        git(directory, "checkout", "-q", "other")
        await refs.begin_turn({"id": task_id}, directory, sink=sink)
    run_db(check, monkeypatch, timeout=60)


def test_end_turn_stores_a_report_says_it_and_raises_no_alert(monkeypatch, tmp_path, native, caplog):
    async def check(db):
        task_id, directory = await make_task(native, db, tmp_path)
        sink = Sink()
        before, branch = await refs.begin_turn({"id": task_id}, directory, sink=sink)
        git(directory, "branch", "other2")
        (directory / "b.txt").write_text("b\n")
        git(directory, "add", "--", "b.txt")
        git(directory, "commit", "-qm", "own")
        task_manager_safety.refusal_counts.clear()
        with caplog.at_level(logging.WARNING):
            report = await refs.end_turn({"id": task_id}, directory, before, branch, sink=sink, message_id="m1")
        assert report.needs_you and report.own_commits == 1
        said, = events_of(sink, "ref_report")
        assert said["needs_you"] is True and said["own_commits"] == 1 and said["head"] is None
        change, = said["changes"]
        assert change["ref"] == "refs/heads/other2" and change["change"] == "created"
        row = await db.fetchrow("SELECT * FROM task_manager_ref_reports WHERE id=$1", said["report_id"])
        assert row["needs_you"] and row["message_id"] == "m1" and row["resolved_at"] is None
        assert json.loads(row["changes"])["changes"][0]["restore"] == change["restore"]
        # A ref change is report-only (RFC 21, 25.10): no refusal is counted and nothing is logged as a problem.
        assert not task_manager_safety.refusal_counts and not [r for r in caplog.records if r.levelno >= logging.WARNING]
        # Nothing was restored: the branch is still there.
        assert git(directory, "rev-parse", "--verify", "refs/heads/other2")
        # An unchanged turn says nothing and stores nothing.
        again = Sink()
        quiet, _ = await refs.begin_turn({"id": task_id}, directory, sink=again), None
        await refs.end_turn({"id": task_id}, directory, quiet[0], branch, sink=again)
        assert again.events == [] and await db.fetchval("SELECT count(*) FROM task_manager_ref_reports") == 1
    run_db(check, monkeypatch, timeout=60)


def test_branch_set_refuses_a_detached_head_a_worktree_task_and_a_missing_directory(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, directory = await make_task(native, db, tmp_path)
        worktree_id, _ = await make_task(native, db, tmp_path, "task-wt", workspace_mode="worktree")
        git(directory, "checkout", "-q", "--detach")
        async with v1_api(tmp_path, db) as call:
            status, _ = await call("tasks.task_manager_branch_set", {"task_id": task_id}, client=True)
            assert status == 403
            for body, expected in (({"task_id": task_id}, 409), ({"task_id": worktree_id}, 409),
                                   ({"task_id": "missing"}, 404)):
                status, answer = await call("tasks.task_manager_branch_set", body)
                assert status == expected, answer
            await db.execute("UPDATE tasks SET work_dir='' WHERE id=$1", task_id)
            status, _ = await call("tasks.task_manager_branch_set", {"task_id": task_id})
            assert status == 409
        assert await db.fetchval("SELECT count(*) FROM task_manager_branches") == 0
    run_db(check, monkeypatch, timeout=60)


def test_operations_are_declared_with_levels_and_short_summaries():
    from looper.agents.api_v1 import document
    operations = {"tasks.task_manager_branch_set"}
    levels = json.loads((Path(__file__).parent / "operation_levels.json").read_text())
    assert all(levels[name] == "inside" for name in operations)
    found = {}
    for path, methods in document()["paths"].items():
        for item in methods.values():
            if item.get("operationId") in operations:
                found[item["operationId"]] = item
    assert set(found) == operations
    for item in found.values():
        assert len(item["summary"]) <= 120 and item["description"].startswith(item["summary"])
    from looper.agents import api_v1
    for name in operations:
        assert api_v1.find("POST", "/api/v1/" + name.replace(".", "/")).operator_only is True


def test_hostile_repository_configuration_runs_nothing_in_begin_end_and_files(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, directory = await make_task(native, db, tmp_path)
        markers = tmp_path / "markers"
        markers.mkdir()
        script = tmp_path / "mark.sh"
        script.write_text('#!/bin/sh\necho "$0" >> "' + str(markers / "ran") + '"\ncat\n')
        script.chmod(0o755)
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        for name in ("post-index-change", "reference-transaction", "post-checkout", "pre-commit"):
            for place in (hooks, directory / ".git" / "hooks"):
                (place / name).write_text(f'#!/bin/sh\necho {name} >> "{markers / "ran"}"\ncat >/dev/null\n')
                (place / name).chmod(0o755)
        git(directory, "config", "core.fsmonitor", str(script))
        git(directory, "config", "core.hooksPath", str(hooks))
        git(directory, "config", "filter.mark.clean", str(script))
        git(directory, "config", "filter.mark.smudge", str(script))
        (directory / ".gitattributes").write_text("*.txt filter=mark\n")
        (directory / "dirty.txt").write_text("dirty\n")
        sink = Sink()
        before, branch = await refs.begin_turn({"id": task_id}, directory, sink=sink)
        git(directory, "-c", "core.hooksPath=/dev/null", "branch", "other3")
        await refs.end_turn({"id": task_id}, directory, before, branch, sink=sink)
        await tm._files(str(directory))
        assert events_of(sink, "ref_report")
        assert not (markers / "ran").exists(), (markers / "ran").read_text()
    run_db(check, monkeypatch, timeout=60)


def test_events_are_stored_with_the_conversation_and_never_reach_the_models_history(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, _ = await make_task(native, db, tmp_path)
        thread = f"task-chat:{task_id}"
        rows = [("user", "hello"), ("event", json.dumps({"kind": "sandbox_notice", "notice": "first_enable_in_place",
                                                        "detail": "d"})),
                ("event", json.dumps({"kind": "ref_report", "report_id": 1, "changes": [], "info": [], "head": None,
                                      "own_commits": 1, "needs_you": False})),
                ("assistant", "answer")]
        for index, (role, content) in enumerate(rows):
            await db.execute("INSERT INTO chat_messages (session_id, message_id, role, content, channel) "
                             "VALUES ($1, $2, $3, $4, 'web')", thread, f"m{index}", role, content)
        history = await task_chat.recent(task_id, 20)
        assert [item["role"] for item in history] == ["user", "assistant"]
        assert "ref_report" not in json.dumps(history) and "sandbox_notice" not in json.dumps(history)
        assert tm.resume_message("next", history).count("ref_report") == 0
    run_db(check, monkeypatch, timeout=60)


def test_a_direct_in_place_turn_reports_refs_after_its_summary_and_the_next_turn_still_runs(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, directory = await make_task(native, db, tmp_path)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            sink = await submit_and_wait(pool, task_id, "sh:git update-ref refs/heads/other HEAD~0 && git branch moved",
                                         message_id="m1")
            kinds = [kind for kind, _ in sink.events]
            assert kinds.index("ref_report") > kinds.index("turn_summary")
            assert kinds.count("sandbox_notice") == 1 and kinds.index("sandbox_notice") < kinds.index("turn_summary")
            said, = events_of(sink, "ref_report")
            assert [c["ref"] for c in said["changes"]] == ["refs/heads/moved"] and said["needs_you"]
            assert await db.fetchval("SELECT count(*) FROM task_manager_ref_reports WHERE task_id=$1", task_id) == 1
            second = await submit_and_wait(pool, task_id, "reply", message_id="m2")  # only HEAD blocks a turn
            assert second.finals == [("onetwo", "success")]
            assert not events_of(second, "sandbox_notice") and not events_of(second, "ref_report")
            third = await submit_and_wait(pool, task_id, "sh:git checkout -q other", message_id="m3")
            assert events_of(third, "ref_report")[0]["head"]["after"] == "refs/heads/other"
            sink = Sink()
            result = await pool.submit(task_id, "again", message_id="m4", sink=sink)
            assert result.state == "refused" and result.reason is tm.RefuseReason.HEAD_MOVED
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=90)


def test_a_failing_report_is_logged_and_recorded_never_raised_over_the_turn(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, directory = await make_task(native, db, tmp_path)
        monkeypatch.setattr(refs, "end_turn", AsyncMock(side_effect=RuntimeError("synthetic")))
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            sink = await submit_and_wait(pool, task_id, "reply", message_id="m1")
            assert sink.finals == [("onetwo", "success")]
            lines = await db.fetch("SELECT summary FROM activity WHERE task_id=$1 AND kind='error'", task_id)
            assert any("Ref report failed" in row["summary"] for row in lines)
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=60)


@pytest.mark.parametrize("mode", ["orchestrated", "worktree"])
def test_orchestrated_and_worktree_turns_never_take_a_ref_snapshot(monkeypatch, tmp_path, native, mode):
    spy = AsyncMock(side_effect=AssertionError("begin_turn must not run"))
    monkeypatch.setattr(refs, "begin_turn", spy)

    async def check(db):
        if mode == "worktree":
            task_id, _ = await make_task(native, db, tmp_path, workspace_mode="worktree")
        else:
            task_id, _ = await make_task(native, db, tmp_path, status="running")
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            await submit_and_wait(pool, task_id, "reply", message_id="m1")
            assert spy.await_count == 0
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=90)


def test_a_task_branch_that_reads_as_an_option_is_never_recorded(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, directory = await make_task(native, db, tmp_path)
        git(directory, "symbolic-ref", "HEAD", "refs/heads/--detach")
        sink = Sink()
        with pytest.raises(tm.Refused) as refused:
            await refs.begin_turn({"id": task_id}, directory, sink=sink)
        assert refused.value.reason is tm.RefuseReason.WORKSPACE and "starts with '-'" in str(refused.value)
        assert await db.fetchval("SELECT count(*) FROM task_manager_branches") == 0 and sink.events == []
        async with v1_api(tmp_path, db) as call:
            status, answer = await call("tasks.task_manager_branch_set", {"task_id": task_id})
            assert status == 409 and "starts with '-'" in json.dumps(answer), answer
        assert await db.fetchval("SELECT count(*) FROM task_manager_branches") == 0
    run_db(check, monkeypatch, timeout=60)


def test_an_unreadable_end_of_turn_snapshot_says_so_in_the_conversation(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, directory = await make_task(native, db, tmp_path)
        sink = Sink()
        before, branch = await refs.begin_turn({"id": task_id}, directory, sink=sink)
        git(directory, "update-ref", "refs/heads/z\u2028refs/heads/main", git(directory, "rev-parse", "HEAD"))
        with pytest.raises(tm.Refused):
            await refs.end_turn({"id": task_id}, directory, before, branch, sink=sink, message_id="m1")
        notices = [p for p in events_of(sink, "sandbox_notice") if p["notice"] == "refs_unreadable"]
        assert len(notices) == 1 and "not reported" in notices[0]["detail"]
        assert not events_of(sink, "ref_report")
    run_db(check, monkeypatch, timeout=60)


def test_a_head_on_a_look_alike_name_or_a_non_branch_ref_is_not_the_task_branch(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, directory = await make_task(native, db, tmp_path)
        sink = Sink()
        before, branch = await refs.begin_turn({"id": task_id}, directory, sink=sink)
        git(directory, "tag", "v1")
        git(directory, "symbolic-ref", "HEAD", "refs/heads/main\u00a0")  # main followed by a no-break space
        assert (await refs.snapshot(directory)).head == "refs/heads/main\u00a0"
        report = await refs.end_turn({"id": task_id}, directory, before, branch, sink=sink, message_id="m1")
        assert report.head_changed
        with pytest.raises(tm.Refused) as refused:
            await refs.begin_turn({"id": task_id}, directory, sink=sink)
        assert refused.value.reason is tm.RefuseReason.HEAD_MOVED
        git(directory, "symbolic-ref", "HEAD", "refs/tags/v1")  # not a branch: the same as a detached HEAD
        assert (await refs.snapshot(directory)).head is None
    run_db(check, monkeypatch, timeout=60)


def test_turn_events_carry_the_turns_message_id(monkeypatch, tmp_path, native):
    async def check(db):
        task_id, directory = await make_task(native, db, tmp_path)
        sink = Sink()
        before, branch = await refs.begin_turn({"id": task_id}, directory, sink=sink)
        git(directory, "tag", "v7")
        await refs.end_turn({"id": task_id}, directory, before, branch, sink=sink, message_id="turn-9")
        assert events_of(sink, "ref_report")[0]["message_id"] == "turn-9"
        git(directory, "update-ref", "refs/heads/z\u2028refs/heads/main", git(directory, "rev-parse", "HEAD"))
        with pytest.raises(tm.Refused):
            await refs.end_turn({"id": task_id}, directory, before, branch, sink=sink, message_id="turn-10")
        assert [p["message_id"] for p in events_of(sink, "sandbox_notice") if p["notice"] == "refs_unreadable"] == ["turn-10"]
    run_db(check, monkeypatch, timeout=60)
