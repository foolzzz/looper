"""Real processes and disposable PostgreSQL exercise the evidence writer."""
from __future__ import annotations

import asyncio
import hashlib
import shlex
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from looper.agents import acceptance_runner as runner
from looper.agents.acceptance_checks import ArtifactCheck, CommandCheck
from looper.agents.project_execution import AcceptanceChecksMode as Mode
from looper.db import plans, projects, step_check_runs as runs, tasks
from tests.db_support import run_db


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=True, timeout=10).stdout.strip()


def repository(root):
    root.mkdir()
    git(root, "init")
    (root / "code.txt").write_text("original\n")
    git(root, "add", "code.txt")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "seed")
    return git(root, "rev-parse", "HEAD")


async def seed(pool, work, checks, *, step_id="step-1"):
    project = await projects.create_project(name="Acceptance", pool=pool)
    await projects.set_acceptance_checks(project["id"], "network", pool=pool)
    await tasks.create_task(task_id="task-check", requirement="Check", goal_id="goal", project_id=project["id"],
                            work_dir=str(work), correlation_id="test", status="running", pool=pool)
    await plans.save("task-check", plan={"plan_id": "plan", "status": "executing"}, steps=[{
        "id": step_id, "index": 0, "instruction": "Check", "acceptance_criteria": ["works"],
        "checks": [check.row() for check in checks]}], pool=pool)
    return project


def invoke(tmp_path, checks, *, work=None, base_commit=None):
    return runner.run_step_checks(task_id="task-check", step_id="step-1", checks=checks,
        mode=Mode.NETWORK, work_dir=work or tmp_path, attempt=1, runtime_root=tmp_path / "runtime",
        base_commit=base_commit)


@pytest.mark.parametrize("code", [0, 1])
def test_exit_output_hash_and_report_only(tmp_path, monkeypatch, caplog, code):
    check = CommandCheck("c1", 1, f"printf measured-output; exit {code}", network=True)
    async def run(pool):
        await seed(pool, tmp_path, [check])
        rows = await invoke(tmp_path, [check])
        row = rows[-1]
        assert row["state"] == ("passed" if code == 0 else "failed")
        assert row["exit_code"] == code and not row["guarded"]
        assert row["output_tail"] == "measured-output"
        assert row["output_bytes"] == 15
        assert row["output_sha256"] == hashlib.sha256(b"measured-output").hexdigest()
        assert "measured-output" not in caplog.text
        await invoke(tmp_path, [check])
        assert len(await runs.list_runs("task-check")) == 1  # resumed DECISION does not execute twice
        await runner.run_step_checks(task_id="task-check", step_id="step-1", checks=[check],
            mode=Mode.NETWORK, work_dir=tmp_path, attempt=2, runtime_root=tmp_path / "runtime")
        assert [r["attempt"] for r in await runs.list_runs("task-check")] == [1, 2]
    run_db(run, monkeypatch)


def test_output_tail_redaction_and_minimal_environment(tmp_path, monkeypatch):
    secret = "not-for-child-environment-12345"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    password = "other-postgres-secret-67890"
    monkeypatch.setenv("PGPASSWORD", password)
    script = ("import os; assert 'ANTHROPIC_API_KEY' not in os.environ; "
              "assert 'PGPASSWORD' not in os.environ; print('x'*5000 + " + repr(secret + password) + ")")
    check = CommandCheck("c1", 1, shlex.join([sys.executable, "-c", script]), network=True)
    async def run(pool):
        await seed(pool, tmp_path, [check])
        row = (await invoke(tmp_path, [check]))[-1]
        data = ("x" * 5000 + secret + password + "\n").encode()
        assert row["state"] == "passed"
        assert len(row["output_tail"]) == 4000 and secret not in row["output_tail"] and password not in row["output_tail"]
        assert row["output_tail"].endswith("[redacted]\n")
        assert row["output_bytes"] == len(data)
        assert row["output_sha256"] == hashlib.sha256(data).hexdigest()
    run_db(run, monkeypatch)


@pytest.mark.parametrize("command,error", [("sleep 10", "timeout"), ("printf 123456789", "output_limit")])
def test_resource_errors(tmp_path, monkeypatch, command, error):
    check = CommandCheck("c1", 1, command, timeout_seconds=1, network=True)
    if error == "output_limit":
        monkeypatch.setattr(runner, "MAX_OUTPUT_BYTES", 8)
    async def run(pool):
        await seed(pool, tmp_path, [check])
        row = (await invoke(tmp_path, [check]))[-1]
        assert (row["state"], row["error_kind"]) == ("error", error)
    run_db(run, monkeypatch)


def test_timeout_ends_background_descendants(tmp_path, monkeypatch):
    child = tmp_path / "child.py"
    child.write_text("import pathlib,time\npathlib.Path('child.pid').write_text(str(__import__('os').getpid()))\ntime.sleep(30)\n")
    check = CommandCheck("c1", 1, shlex.join([sys.executable, str(child)]) + " & wait", timeout_seconds=1, network=True)
    async def run(pool):
        await seed(pool, tmp_path, [check])
        row = (await invoke(tmp_path, [check]))[-1]
        assert row["error_kind"] == "timeout"
        pid = int((tmp_path / "child.pid").read_text())
        listed = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        assert not listed.stdout.strip() or listed.stdout.strip().startswith("Z")
    run_db(run, monkeypatch)


@pytest.mark.parametrize("command", ["printf modified > code.txt", "cp code.txt saved; printf changed > code.txt; cp saved code.txt"])
def test_private_checkout_detects_even_restored_writes(tmp_path, monkeypatch, command):
    work = tmp_path / "repo"
    head = repository(work)
    check = CommandCheck("c1", 1, command, protect=("code.txt",), network=True)
    async def run(pool):
        await seed(pool, work, [check])
        row = (await invoke(tmp_path, [check], work=work, base_commit=head))[-1]
        assert row["guarded"] and row["head_sha"] == head and row["content_sha"]
        assert (row["state"], row["error_kind"]) == ("error", "changed_code")
        assert row["protected_changed"] == ["code.txt"]
        assert (work / "code.txt").read_text() == "original\n"
        assert git(work, "status", "--porcelain") == ""
        assert not list((tmp_path / "runtime" / ".local" / "acceptance").glob("*"))
    run_db(run, monkeypatch)


@pytest.mark.parametrize("command,state", [
    ('test "$(cat linked.txt)" = original', "passed"),
    ("printf changed > linked.txt", "error"),
])
def test_internal_absolute_symlink_evidence_identifies_source_version(tmp_path, monkeypatch, command, state):
    from looper.agents.acceptance_gate import gate
    from looper.agents.tiers.worker import _verification_snapshot

    work = tmp_path / "repo"
    repository(work)
    (work / "linked.txt").symlink_to(work / "code.txt")
    git(work, "add", "linked.txt")
    git(work, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "internal link")
    source = _verification_snapshot(str(work))
    check = CommandCheck("c1", 1, command, network=True)
    async def run(pool):
        await seed(pool, work, [check])
        row = (await invoke(tmp_path, [check], work=work, base_commit=source["head"]))[-1]
        assert row["guarded"] and row["state"] == state
        assert (row["head_sha"], row["content_sha"]) == (source["head"], source["content_sha256"])
        assert gate([check], [row], (source["head"], source["content_sha256"])).passed is (state == "passed")
        if state == "error":
            assert row["error_kind"] == "changed_code"
        assert _verification_snapshot(str(work)) == source
        assert (work / "code.txt").read_text() == "original\n"
    run_db(run, monkeypatch)


def test_protected_paths_compare_to_step_start_commit(tmp_path, monkeypatch):
    work = tmp_path / "repo"
    head = repository(work)
    (work / "code.txt").write_text("worker edited test\n")
    git(work, "add", "code.txt")
    git(work, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "worker")
    check = CommandCheck("c1", 1, "true", protect=("code.txt",), network=True)
    async def run(pool):
        await seed(pool, work, [check])
        row = (await invoke(tmp_path, [check], work=work, base_commit=head))[-1]
        assert row["state"] == "failed" and row["protected_changed"] == ["code.txt"]
    run_db(run, monkeypatch)


@pytest.mark.parametrize("command,state,error", [
    ("true", "failed", None),
    ("ln -s code.txt result.txt", "error", "artifact_invalid"),
    ("mkdir result.txt", "error", "artifact_invalid"),
    ("printf '\\000bad' > result.txt", "error", "artifact_invalid"),
    ("head -c 2097153 /dev/zero > result.txt", "error", "artifact_invalid"),
    ("mkdir out; ln -s .. out/link; printf x > out/link/result.txt", "error", "artifact_invalid"),
])
def test_invalid_artifacts(tmp_path, monkeypatch, command, state, error):
    work = tmp_path / "repo"
    head = repository(work)
    path = "out/link/result.txt" if command.startswith("mkdir out") else "result.txt"
    check = ArtifactCheck("c1", 1, command, artifact=(path,), network=True)
    async def run(pool):
        await seed(pool, work, [check])
        row = (await invoke(tmp_path, [check], work=work, base_commit=head))[-1]
        assert (row["state"], row["error_kind"]) == (state, error)
        assert row["artifacts"] == []
    run_db(run, monkeypatch)


def test_artifacts_are_content_typed_retained_and_deleted_with_task(tmp_path, monkeypatch):
    check = ArtifactCheck("c1", 1, "printf '{\"ok\": true}' > misleading.png", artifact=("misleading.png",), network=True)
    async def run(pool):
        await seed(pool, tmp_path, [check])
        row = (await invoke(tmp_path, [check]))[-1]
        assert row["state"] == "passed"
        item = row["artifacts"][0]
        assert set(item) == {"name", "mime", "bytes", "sha256"}
        assert item["mime"] == "application/json" and item["name"] == "1.json"
        files = list((tmp_path / "runtime" / "evidence").rglob("*.json"))
        assert len(files) == 1 and files[0].read_text() == '{"ok": true}'
        await tasks.update_task_status("task-check", "completed", pool=pool)
        from looper.agents.projects import delete_task
        await delete_task("task-check", tmp_path / "runtime")
        assert not list((tmp_path / "runtime" / "evidence").rglob("*.json"))
        assert await runs.list_runs("task-check") == []
    run_db(run, monkeypatch)


@pytest.mark.parametrize("failure", ["sandbox", "monitor", "evidence"])
def test_preparation_failure_never_runs_check(tmp_path, monkeypatch, failure):
    work = tmp_path / "repo"
    head = repository(work)
    marker = tmp_path / "must-not-run"
    check = CommandCheck("c1", 1, f"touch {shlex.quote(str(marker))}", network=failure != "sandbox")
    if failure == "sandbox":
        monkeypatch.setattr(runner, "local_prefix", AsyncMock(return_value=None))
    elif failure == "monitor":
        monkeypatch.setattr(runner.VerificationWrites, "start", lambda _: (_ for _ in ()).throw(OSError("unavailable")))
    else:
        monkeypatch.setattr(runs, "create_run", AsyncMock(side_effect=OSError("database unavailable")))
    async def run(pool):
        await seed(pool, work, [check])
        if failure == "evidence":
            with pytest.raises(runner.EvidenceUnavailable):
                await invoke(tmp_path, [check], work=work, base_commit=head)
        else:
            row = (await invoke(tmp_path, [check], work=work, base_commit=head))[-1]
            assert row["error_kind"] == ("sandbox_unavailable" if failure == "sandbox" else "evidence_unavailable")
        assert not marker.exists()
    run_db(run, monkeypatch)


def test_budget_records_remaining_checks_and_disabled_switch_stops_execution(tmp_path, monkeypatch):
    checks = [CommandCheck("c1", 1, "true", network=True), CommandCheck("c2", 1, "touch never", network=True)]
    monkeypatch.setattr(runner, "STEP_BUDGET_SECONDS", 0)
    async def run(pool):
        project = await seed(pool, tmp_path, checks)
        rows = await invoke(tmp_path, checks)
        assert [r["error_kind"] for r in rows] == ["budget", "budget"]
        assert not (tmp_path / "never").exists()
        await projects.set_acceptance_checks(project["id"], None)
        assert await runner.run_step_checks(task_id="task-check", step_id="step-1", checks=checks,
            mode=Mode.NETWORK, work_dir=tmp_path, attempt=2, runtime_root=tmp_path / "runtime") == []
    run_db(run, monkeypatch)


def test_interrupted_restart_retries_only_unfinished_checks(tmp_path, monkeypatch):
    checks = [CommandCheck("c1", 1, "echo once >> count", network=True), CommandCheck("c2", 1, "true", network=True)]
    async def run(pool):
        await seed(pool, tmp_path, checks)
        row = await runs.create_run("task-check", "step-1", checks[0].row(), 1)
        await runs.finish_run(row["id"], runs.CheckState.PASSED)
        await runs.create_run("task-check", "step-1", checks[1].row(), 1)
        # A day offline is not a day spent executing this check.
        await pool.execute("UPDATE step_check_runs SET started_at=now()-interval '1 day' WHERE state='running'")
        await runner.recover(tmp_path / "runtime")
        rows = await invoke(tmp_path, checks)
        assert [(r["check_id"], r["state"]) for r in rows] == [("c1", "passed"), ("c2", "error"), ("c2", "passed")]
        assert rows[1]["error_kind"] == "interrupted"
        assert not (tmp_path / "count").exists()
    run_db(run, monkeypatch)


def test_recheck_resumes_only_checks_not_yet_rerun(tmp_path, monkeypatch):
    checks = [CommandCheck("c1", 1, "echo c1 >> count", network=True),
              CommandCheck("c2", 1, "echo c2 >> count", network=True)]
    async def run(pool):
        await seed(pool, tmp_path, checks)
        await invoke(tmp_path, checks)
        options = dict(task_id="task-check", step_id="step-1", mode=Mode.NETWORK,
            work_dir=tmp_path, attempt=1, runtime_root=tmp_path / "runtime", rerun_after={"c1": 1, "c2": 1})
        # A restart after the first check of this recheck must not execute it twice.
        await runner.run_step_checks(checks=checks[:1], **options)
        await runner.run_step_checks(checks=checks, **options)
        await invoke(tmp_path, checks)
        rows = await runs.list_runs("task-check")
        assert [(r["check_id"], r["attempt"], r["run"]) for r in rows] == [
            ("c1", 1, 1), ("c2", 1, 1), ("c1", 1, 2), ("c2", 1, 2)]
        assert (tmp_path / "count").read_text() == "c1\nc2\nc1\nc2\n"
        await runner.run_step_checks(checks=checks, **{**options, "rerun_after": {"c1": 2, "c2": 2}})
        assert [r["run"] for r in await runs.list_runs("task-check")] == [1, 1, 2, 2, 3, 3]
    run_db(run, monkeypatch)


@pytest.mark.parametrize("attempt", [1, 2])
def test_step_budget_includes_previous_runs_and_worker_attempts(tmp_path, monkeypatch, attempt):
    check = CommandCheck("c1", 1, "touch must-not-run", network=True)
    async def run(pool):
        await seed(pool, tmp_path, [check])
        old = await runs.create_run("task-check", "step-1", check.row(), 1)
        await runs.finish_run(old["id"], runs.CheckState.ERROR, error_kind=runs.ErrorKind.TIMEOUT,
            duration_ms=runner.STEP_BUDGET_SECONDS * 1000)
        rows = await runner.run_step_checks(task_id="task-check", step_id="step-1", checks=[check],
            mode=Mode.NETWORK, work_dir=tmp_path, attempt=attempt, runtime_root=tmp_path / "runtime",
            rerun_after={"c1": 1} if attempt == 1 else None)
        assert rows[-1]["state"] == "error" and rows[-1]["error_kind"] == "budget"
        assert not (tmp_path / "must-not-run").exists()
    run_db(run, monkeypatch)


def test_stale_evidence_is_appended_without_rewriting_measurements(tmp_path, monkeypatch):
    check = CommandCheck("c1", 1, "true", network=True)
    async def run(pool):
        await seed(pool, tmp_path, [check])
        old = await runs.create_run("task-check", "step-1", check.row(), 1,
            head_sha="measured-head", content_sha="measured-content", guarded=True)
        measured = await runs.finish_run(old["id"], runs.CheckState.PASSED, exit_code=0, duration_ms=50,
            output_tail="measured output", output_bytes=15, output_sha256="output-digest")
        stale, = await runner.mark_stale(task_id="task-check", step_id="step-1", checks=[check],
            attempt=1, current=[measured])
        assert (stale["state"], stale["attempt"], stale["run"]) == ("stale", 1, 2)
        for name in ("head_sha", "content_sha", "guarded", "exit_code", "output_tail", "output_bytes", "output_sha256"):
            assert stale[name] == measured[name]
        assert stale["duration_ms"] == 0  # invalidation consumes no execution budget
        assert (await runs.list_runs("task-check"))[0] == measured
        assert await runner.mark_stale(task_id="task-check", step_id="step-1", checks=[check],
            attempt=1, current=[stale]) == []
        await runner.run_step_checks(task_id="task-check", step_id="step-1", checks=[check],
            mode=Mode.NETWORK, work_dir=tmp_path, attempt=1, runtime_root=tmp_path / "runtime",
            rerun_after={"c1": 2})
        assert [r["state"] for r in await runs.list_runs("task-check")] == ["passed", "stale", "passed"]
    run_db(run, monkeypatch)


def test_disabled_recheck_returns_before_reading_evidence(tmp_path, monkeypatch):
    history = AsyncMock(side_effect=AssertionError("disabled runner read evidence"))
    monkeypatch.setattr(runs, "list_runs", history)
    async def run():
        assert await runner.run_step_checks(task_id="task-check", step_id="step-1",
            checks=[CommandCheck("c1", 1, "touch must-not-run")], mode=Mode.OFF, work_dir=tmp_path,
            attempt=1, runtime_root=tmp_path / "runtime", rerun_after={"c1": 1}) == []
        history.assert_not_awaited()
        assert not (tmp_path / "must-not-run").exists()
    asyncio.run(run())


def test_failed_check_does_not_skip_later_checks(tmp_path, monkeypatch):
    checks = [CommandCheck("c1", 1, "exit 1", network=True),
              CommandCheck("c2", 1, "printf still-runs", network=True)]
    async def run(pool):
        await seed(pool, tmp_path, checks)
        rows = await invoke(tmp_path, checks)
        assert [(r["check_id"], r["state"]) for r in rows] == [("c1", "failed"), ("c2", "passed")]
        assert rows[1]["output_tail"] == "still-runs"
    run_db(run, monkeypatch)


def test_artifact_budget_discards_oldest_round_but_keeps_rows(tmp_path, monkeypatch):
    from looper.agents import acceptance_artifacts
    monkeypatch.setattr(acceptance_artifacts, "MAX_TASK_ARTIFACT_BYTES", 8)
    check = ArtifactCheck("c1", 1, "printf artifact > result.txt", artifact=("result.txt",), network=True)
    async def run(pool):
        await seed(pool, tmp_path, [check])
        for attempt in (1, 2):
            await runner.run_step_checks(task_id="task-check", step_id="step-1", checks=[check], mode=Mode.NETWORK,
                work_dir=tmp_path, attempt=attempt, runtime_root=tmp_path / "runtime")
        rows = await runs.list_runs("task-check")
        assert len(rows) == 2 and all(r["state"] == "passed" and r["artifacts"] for r in rows)
        files = list((tmp_path / "runtime" / "evidence").rglob("*.txt"))
        assert len(files) == 1 and files[0].relative_to(tmp_path / "runtime" / "evidence").parts[2] == "2"
    run_db(run, monkeypatch)


def test_process_fence_records_error_without_launching(tmp_path, monkeypatch):
    from looper.agents import proc_tree
    monkeypatch.setattr(proc_tree, "spawn", AsyncMock(side_effect=RuntimeError("control plane fenced")))
    check = CommandCheck("c1", 1, "touch forbidden", network=True)
    async def run(pool):
        await seed(pool, tmp_path, [check])
        row = (await invoke(tmp_path, [check]))[-1]
        assert row["error_kind"] == "evidence_unavailable" and not (tmp_path / "forbidden").exists()
    run_db(run, monkeypatch)


@pytest.mark.parametrize("stage", ["preflight", "monitor", "artifact"])
def test_unexpected_failure_settles_evidence_before_propagating(tmp_path, monkeypatch, stage):
    work = tmp_path / "repo"
    head = repository(work)
    check = ArtifactCheck("c1", 1, "printf measured > result.txt", artifact=("result.txt",), network=stage != "preflight")
    if stage == "preflight":
        from looper.agents import proc_tree
        monkeypatch.setattr(proc_tree, "spawn", AsyncMock(side_effect=RuntimeError("control plane fenced")))
    elif stage == "monitor":
        monkeypatch.setattr(runner.VerificationWrites, "finish", lambda _: (_ for _ in ()).throw(RuntimeError("watch lost")))
    else:
        monkeypatch.setattr(runner.artifacts, "collect", lambda *a: (_ for _ in ()).throw(RuntimeError("storage lost")))

    async def run(pool):
        await seed(pool, work, [check])
        with pytest.raises(RuntimeError):
            await invoke(tmp_path, [check], work=work, base_commit=head)
        row, = await runs.list_runs("task-check")
        assert (row["state"], row["error_kind"]) == ("error", "evidence_unavailable")
        assert row["finished_at"] is not None and row["duration_ms"] is not None
        assert await runs.recover_interrupted() == []  # a completed failure is not a restart interruption
        assert not (work / "result.txt").exists()
    run_db(run, monkeypatch)


@pytest.mark.parametrize("stage", ["result", "preflight"])
def test_failed_evidence_write_remains_unfinished_and_cannot_be_skipped(tmp_path, monkeypatch, stage):
    check = CommandCheck("c1", 1, "echo measured >> count", network=stage == "result")
    if stage == "preflight":
        from looper.agents import proc_tree
        monkeypatch.setattr(proc_tree, "spawn", AsyncMock(side_effect=RuntimeError("control plane fenced")))
    finish = AsyncMock(side_effect=OSError("evidence database unavailable"))
    monkeypatch.setattr(runs, "finish_run", finish)
    async def run(pool):
        await seed(pool, tmp_path, [check])
        with pytest.raises(runner.EvidenceUnavailable):
            await invoke(tmp_path, [check])
        row, = await runs.list_runs("task-check")
        assert row["state"] == "running" and row["finished_at"] is None
        with pytest.raises(runner.EvidenceUnavailable, match="running"):
            await invoke(tmp_path, [check])
        if stage == "result":
            assert (tmp_path / "count").read_text() == "measured\n"
        else:
            assert not (tmp_path / "count").exists()
        assert finish.await_count == 1  # do not retry an uncertain terminal write
    run_db(run, monkeypatch)


@pytest.mark.parametrize("network,error", [(True, "could_not_start"), (False, "sandbox_unavailable")])
def test_launch_failure_distinguishes_network_and_sandbox(tmp_path, monkeypatch, network, error):
    from looper.agents import proc_tree
    monkeypatch.setattr(proc_tree, "spawn", AsyncMock(side_effect=FileNotFoundError("launcher missing")))
    check = CommandCheck("c1", 1, "touch must-not-run", network=network)
    async def run(pool):
        await seed(pool, tmp_path, [check])
        row = (await invoke(tmp_path, [check]))[-1]
        assert (row["state"], row["error_kind"]) == ("error", error)
        assert not (tmp_path / "must-not-run").exists()
    run_db(run, monkeypatch)


def test_permission_downgrade_refuses_network_check_without_claiming_sandbox_failure(tmp_path, monkeypatch):
    check = CommandCheck("c1", 1, "touch must-not-run", network=True)
    async def run(pool):
        owner = await seed(pool, tmp_path, [check])
        await projects.set_acceptance_checks(owner["id"], "local", pool=pool)
        row = (await invoke(tmp_path, [check]))[-1]
        assert (row["state"], row["error_kind"]) == ("error", "could_not_start")
        assert not (tmp_path / "must-not-run").exists()
    run_db(run, monkeypatch)


def test_remaining_budget_expires_before_command_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "STEP_BUDGET_SECONDS", 0.25)
    check = CommandCheck("c1", 1, "sleep 10", timeout_seconds=10, network=True)
    async def run(pool):
        await seed(pool, tmp_path, [check])
        row = (await invoke(tmp_path, [check]))[-1]
        assert (row["state"], row["error_kind"]) == ("error", "budget")
    run_db(run, monkeypatch)


def test_task_artifact_removal_waits_for_an_active_collection(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from looper.agents import acceptance_artifacts as artifacts

    source, runtime = tmp_path / "source", tmp_path / "runtime"
    source.mkdir()
    runtime.mkdir()
    (source / "report.txt").write_text("measured")
    collecting, release, deleting = threading.Event(), threading.Event(), threading.Event()
    original = artifacts._mkdir
    def pause(directory, boundary):
        original(directory, boundary)
        collecting.set()
        assert release.wait(5)
    monkeypatch.setattr(artifacts, "_mkdir", pause)
    def remove():
        deleting.set()
        artifacts.remove_task(runtime, "task-check")
    with ThreadPoolExecutor(max_workers=2) as threads:
        collected = threads.submit(artifacts.collect, source, ("report.txt",), runtime,
            {"task_id": "task-check", "step_id": "step-1", "attempt": 1, "check_id": "c1", "run": 1})
        try:
            assert collecting.wait(5)
            removed = threads.submit(remove)
            assert deleting.wait(5)
            with pytest.raises(TimeoutError):
                removed.result(timeout=0.1)
        finally:
            release.set()
        assert collected.result(timeout=5)[0]["bytes"] == 8
        removed.result(timeout=5)
    assert not artifacts.task_directory(runtime, "task-check").exists()


def test_service_concurrency_is_two_across_steps(tmp_path, monkeypatch):
    async def run(pool):
        check = CommandCheck("c1", 1, "sleep .2", network=True)
        await seed(pool, tmp_path, [check])
        await plans.save("task-check", steps=[{"id": f"step-{i}", "index": i, "instruction": "check",
            "acceptance_criteria": ["works"], "checks": [check.row()]} for i in (2, 3)])
        active = peak = 0
        original = runner._execute
        async def observed(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                return await original(*args, **kwargs)
            finally:
                active -= 1
        monkeypatch.setattr(runner, "_execute", observed)
        await asyncio.gather(*(runner.run_step_checks(task_id="task-check", step_id=f"step-{i}", checks=[check],
            mode=Mode.NETWORK, work_dir=tmp_path, attempt=1, runtime_root=tmp_path / "runtime") for i in (1, 2, 3)))
        assert peak == 2
    run_db(run, monkeypatch)
