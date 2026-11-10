"""Review panels settle through dependency conditions on the Planner's bus path."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents.message import MessageType
from looper.db import task_roles
from tests.test_planner_agent import (FINAL_RESULT, REVIEW_PASS, WORK_OK, _project, chat, decide, drive, plan, step)

MEMBERS = ["review_a", "review_b", "review_c"]
PANEL = {"key": "panel", "name": "Review panel", "review_panel": {
    "members": MEMBERS, "accept_when": {"all_of": ["review_a", "review_b"]}},
    "distinct_reviewer_runtime": True,
    "roles": [{"key": member, "kind": "review", "role_prompt": "Review the work."} for member in MEMBERS]}
BLOCK = {"passed": False, "summary": "Needs a fix", "findings": [
    {"severity": "blocking", "where": "calc.py:4", "text": "Handle empty input"}]}


def configure(agent_db: Any, monkeypatch: pytest.MonkeyPatch, **changes: Any) -> None:
    workflow = {**PANEL, **changes}
    agent_db.get_task_project.return_value = {**_project(), "task_workflow": workflow}
    rows = [{"task_id": "t1", "role_key": member, "version": 1, "kind": "review", "via": "workflow",
             "role_prompt": "Review the work.", "harness": None, "profile_id": None,
             "reason": "workflow", "profile_name": None, "profile_allowed": False} for member in MEMBERS]
    monkeypatch.setattr(task_roles, "current", AsyncMock(return_value=rows))
    monkeypatch.setattr(task_roles, "get", AsyncMock(side_effect=lambda _, key, *args, **kw: next(
        row for row in rows if row["role_key"] == key)))


def held_reviews(envelope):
    return None if envelope.message_type == MessageType.VERIFY_REQUEST else WORK_OK


def test_required_reviews_release_accept_before_advisory_but_delivery_waits(
    planner_claude, agent_db, monkeypatch,
):
    configure(agent_db, monkeypatch)
    claude = planner_claude(plan(step("s1", review=True)), decide("review"), decide("accept"))

    def outcome(envelope):
        return None if envelope.message_type == MessageType.VERIFY_REQUEST else {
            **WORK_OK, "runtime_id": "runtime-from-worker-run"}

    async def scenario(h):
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        assert len(h.held) == 3
        assert all(e.payload["severity"] for e in h.reviews)
        assert all(e.payload["worker_runtime_id"] == "runtime-from-worker-run" for e in h.reviews)
        await h.release(REVIEW_PASS)
        await h.release(REVIEW_PASS)
        await h.until(lambda: h.planner._plan.steps[0].status == "verified")
        assert h.final() is None
        assert h.planner._plan.progress_summary() == "1/1 完成"
        await h.release(REVIEW_PASS)
        final = await h.finished()
        assert final.payload["status"] == "completed"
        assert final.payload["summary"] == FINAL_RESULT
    drive(claude, outcome, scenario)


def test_failed_required_seat_waits_for_all_required_and_merges_locations(planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch)
    claude = planner_claude(plan(step("s1", review=True)), decide("review"), decide("fail", reason="Fix both"))

    async def scenario(h):
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        await h.release(BLOCK)
        await h.until(lambda: h.planner._plan.steps[1].status == "verified")
        assert len(claude.turns()) == 2
        await h.release({**BLOCK, "findings": [{**BLOCK["findings"][0], "where": "calc.py:9"}]})
        await h.finished()
        report = claude.turns()[-1]["text"]
        assert all(text in report for text in ("calc.py:4", "calc.py:9", "review_a", "review_b"))
        findings = h.planner._plan.steps[0].result["verify_findings"]
        assert [(f["member"], f["severity"], f["where"], f["text"]) for f in findings] == [
            ("review_a", "blocking", "calc.py:4", "Handle empty input"),
            ("review_b", "blocking", "calc.py:9", "Handle empty input")]
    drive(claude, held_reviews, scenario)


def test_k_of_releases_after_two_passes_and_keeps_last_review_running(planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch, review_panel={"members": MEMBERS, "accept_when": {"k_of": 2, "of": MEMBERS}})
    claude = planner_claude(plan(step("s1", review=True)), decide("review"), decide("accept"))

    async def scenario(h):
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        await h.release(REVIEW_PASS)
        await h.release(REVIEW_PASS)
        await h.until(lambda: h.planner._plan.steps[0].status == "verified")
        assert len(h.held) == 1 and h.final() is None
        await h.release(REVIEW_PASS)
        await h.finished()
    drive(claude, held_reviews, scenario)


@pytest.mark.parametrize("condition", [
    {"all_of": MEMBERS[:2]},
    {"k_of": 2, "of": MEMBERS},
    {"any_of": [{"all_of": MEMBERS[:2]}, MEMBERS[2]]},
])
def test_late_blocking_gets_a_decision_that_can_retry(condition, planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch, review_panel={"members": MEMBERS, "accept_when": condition})
    claude = planner_claude(plan(step("s1", review=True)), decide("review"), decide("accept"),
                            decide("retry", feedback="Handle the late empty-input finding"),
                            decide("review"), decide("accept"))

    async def scenario(h):
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        await h.release(REVIEW_PASS)
        await h.release(REVIEW_PASS)
        await h.until(lambda: h.planner._plan.steps[0].status == "verified")
        await h.release(BLOCK)
        await h.until(lambda: len(h.reviews) == 6)
        message = claude.turns()[3]["text"]
        assert "[DECISION]" in message
        assert all(text in message for text in ("review_c", "calc.py:4", "Handle empty input", "blocking"))
        assert any(entry.kind.value == "late_review" for entry in h.planner._plan.journal)
        assert len(h.work_items) == 2
        assert h.work_items[-1].payload["feedback"] == ["Handle the late empty-input finding"]
        for _ in MEMBERS:
            await h.release(REVIEW_PASS)
        assert (await h.finished()).payload["status"] == "completed"
    drive(claude, held_reviews, scenario)


@pytest.mark.parametrize("condition", [{"all_of": MEMBERS[:2]}, {"k_of": 2, "of": MEMBERS}])
@pytest.mark.parametrize("removed", [False, True], ids=["retained-worktree", "removed-worktree"])
def test_late_blocker_retries_merged_work_in_a_fresh_workspace(
    condition, removed, planner_claude, agent_db, monkeypatch,
):
    from looper.agents.step_workspaces import StepWorkspaceState

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    configure(agent_db, monkeypatch, review_panel={"members": MEMBERS, "accept_when": condition})
    agent_db.get_task_project.return_value["step_workspaces"] = "worktree"
    claude = planner_claude(plan(step("s1", review=True), step("s2")),
        decide("review"), decide("accept"), decide("retry", feedback="Fix the late blocker"),
        decide("review"), decide("accept"), decide("accept"))
    attempts = []

    def git(where, *args):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", *args],
            cwd=where, check=True, capture_output=True, text=True, timeout=10).stdout.strip()

    def outcome(envelope):
        if envelope.message_type == MessageType.VERIFY_REQUEST or envelope.payload["step_key"] == "s2":
            return None
        context = envelope.payload["context"]
        directory = Path(context["work_dir"])
        attempts.append({"context": context, "head": git(directory, "rev-parse", "HEAD"),
                         "other": (directory / "other.txt").exists()})
        (directory / "output.txt").write_text(f"attempt {len(attempts)}\n")
        return WORK_OK

    async def scenario(h):
        h.work_dir.mkdir()
        git(h.work_dir, "init", "-q", "-b", "main")
        git(h.work_dir, "config", "user.name", "Example")
        git(h.work_dir, "config", "user.email", "dev@example.org")
        git(h.work_dir, "commit", "--allow-empty", "-qm", "Initial")
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        # Keep the parallel worker running while the panel accepts the first attempt.
        parallel = next(e for e in h.held if e.message_type == MessageType.WORK_ITEM)
        h.held.remove(parallel)
        await h.release(REVIEW_PASS)
        await h.release(REVIEW_PASS)
        target = h.planner._plan.steps[0]
        await h.until(lambda: target.status == "verified")
        old = target.workspace
        assert old.state is StepWorkspaceState.MERGED and Path(old.path).exists()
        assert (h.work_dir / "output.txt").read_text() == "attempt 1\n"
        if removed:
            target.workspace = await h.planner._remove_workspace(old)
        # The retry must include work that landed after the first attempt was merged.
        (h.work_dir / "other.txt").write_text("later work\n")
        git(h.work_dir, "add", "other.txt")
        git(h.work_dir, "commit", "-qm", "Later work")
        latest = git(h.work_dir, "rev-parse", "HEAD")
        await h.release(BLOCK)
        await h.until(lambda: len(h.reviews) == 6)
        fresh = target.workspace
        assert fresh.id != old.id and fresh.state is StepWorkspaceState.ACTIVE
        assert fresh.base_commit == attempts[1]["head"] == latest
        assert attempts[1]["other"] and not Path(old.path).exists()
        assert fresh.result_commit != old.result_commit
        for review in h.reviews[3:]:
            assert review.payload["head"] == fresh.result_commit
            assert git(review.payload["context"]["work_dir"], "show", "HEAD:output.txt") == "attempt 2"
        for _ in MEMBERS:
            await h.release(REVIEW_PASS)
        await h.until(lambda: target.status == "verified")
        assert (h.work_dir / "output.txt").read_text() == "attempt 2\n"
        assert git(h.work_dir, "show", "HEAD:output.txt") == "attempt 2"
        await h.answer(parallel, WORK_OK)
        assert (await h.finished()).payload["status"] == "completed"

    drive(claude, outcome, scenario)


def test_suggestions_are_merged_shown_and_recorded_without_losing_delivery(planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch)
    suggestion = {"severity": "suggestion", "where": "calc.py:7", "text": "Clarify the parameter name"}
    choice = "Decline review_a finding 0: the public API keeps its existing parameter name"
    claude = planner_claude(plan(step("s1", review=True)), decide("review"), decide("accept", summary=choice))

    async def scenario(h):
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        await h.release({**REVIEW_PASS, "findings": [suggestion]})
        await h.release(REVIEW_PASS)
        await h.until(lambda: h.planner._plan.steps[0].status == "verified")
        message = claude.turns()[-1]["text"]
        assert all(text in message for text in ("suggestion", "review_a", suggestion["where"], suggestion["text"]))
        assert "take or decline" in message
        late = {**suggestion, "where": "calc.py:9", "text": "Add a usage example"}
        await h.release({**REVIEW_PASS, "findings": [late]})
        final = await h.finished()
        assert final.payload["status"] == "completed" and final.payload["summary"] == FINAL_RESULT
        assert len(claude.turns()) == 3  # a late suggestion records advice without another decision
        stored = await agent_db.load_plan("t1")
        result = stored["steps"][0]["result"]
        assert result["verify_passed"] is True
        assert result["verify_findings"] == [{"member": "review_a", **suggestion}, {"member": "review_c", **late}]
        assert any(choice in entry.summary for entry in h.planner._plan.journal)
        assert any(entry.kind.value == "late_review" and late["text"] in entry.summary
                   for entry in h.planner._plan.journal)
    drive(claude, held_reviews, scenario)


def test_retry_stops_old_reviews_and_resets_the_round(planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch)
    claude = planner_claude(plan(step("s1", review=True)), decide("review"),
                            decide("retry", feedback="Handle empty input"), decide("review"), decide("accept"))

    async def scenario(h):
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        await h.release(BLOCK)
        await h.release(REVIEW_PASS)
        await h.until(lambda: len(h.reviews) == 6)
        assert len(h.killed) == 1
        assert all(s.retries == 1 for s in h.planner._plan.steps if s.reviews)
        for _ in MEMBERS:
            await h.release(REVIEW_PASS)
        await h.finished()
    drive(claude, held_reviews, scenario)


def test_adjudication_requires_reason_then_rechecks_acceptance(planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch, adjudication=True)
    judgment = {"member": "review_a", "finding": 0, "level": "suggestion", "reason": "Already covered by validation"}
    claude = planner_claude(plan(step("s1", review=True)), decide("review"),
                            decide("accept", adjudications=[{**judgment, "reason": " "}]),
                            decide("accept", adjudications=[judgment]))

    async def scenario(h):
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        await h.release(BLOCK)
        await h.release(REVIEW_PASS)
        await h.until(lambda: h.planner._plan.steps[0].status == "verified")
        assert h.planner._plan.steps[1].result["status"] == "passed_by_adjudication"
        assert h.planner._plan.steps[0].result["verify_findings"][0]["severity"] == "suggestion"
        assert h.planner._plan.steps[0].result["verify_passed"] is True
        assert any(e.kind.value == "review_adjudicated" for e in h.planner._plan.journal)
        assert "reason" in claude.turns()[-1]["text"]
        await h.release(REVIEW_PASS)
        await h.finished()
    drive(claude, held_reviews, scenario)


def test_failed_panel_cannot_be_accepted_without_adjudication(planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch)
    claude = planner_claude(plan(step("s1", review=True)), decide("review"), decide("accept"),
                            decide("fail", reason="Required review failed"))

    async def scenario(h):
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        await h.release(BLOCK)
        await h.release(REVIEW_PASS)
        assert (await h.finished()).payload["status"] == "failed"
        assert "accept is not allowed" in claude.turns()[-1]["text"]
    drive(claude, held_reviews, scenario)


@pytest.mark.parametrize("review_roles", [[], ["review_a"]])
def test_planner_cannot_override_fixed_panel(review_roles):
    from looper.agents.task_roles import RoleKind
    from looper.agents.tiers.planner_protocol import ProtocolError, parse_plan
    spec = {**step("s1", review=True), "review_roles": review_roles}
    with pytest.raises(ProtocolError, match="the workflow fixes the review panel"):
        parse_plan(plan(spec), {"review_a": RoleKind.REVIEW}, review_panel=True)


def test_replan_drops_target_and_its_running_advisory(planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch)
    claude = planner_claude(plan(step("s1", review=True)), decide("review"),
                            decide("replan", steps=[step("s2")]), decide("accept"))

    async def scenario(h):
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        await h.release(BLOCK)
        await h.release(REVIEW_PASS)
        await h.finished()
        assert len(h.killed) == 1
        assert all(s.status == "skipped" for s in h.planner._plan.steps if s.reviews)
    drive(claude, held_reviews, scenario)


def test_restart_redispatches_reviews_and_ignores_old_run_results(planner_claude, agent_db, monkeypatch):
    from looper.agents.message import MessageEnvelope, TASK_ROUTER_ADDRESS
    configure(agent_db, monkeypatch)
    claude = planner_claude(plan(step("s1", review=True)), decide("review"), decide("accept"))

    async def scenario(h):
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        await h.bus.publish(MessageEnvelope.create(source=TASK_ROUTER_ADDRESS, target=h.planner.agent_address,
                                                   message_type=MessageType.TASK_RESUME, payload={}))
        await h.until(lambda: len(h.reviews) == 6)
        for _ in MEMBERS:
            await h.release(BLOCK)  # messages from the old processes cannot settle the new seats
        for _ in MEMBERS:
            await h.release(REVIEW_PASS)
        assert (await h.finished()).payload["status"] == "completed"
        assert all(s.retries == 0 for s in h.planner._plan.steps)
    drive(claude, held_reviews, scenario)


def test_panel_contract_is_opt_in_and_exposes_adjudication_only_when_enabled():
    import hashlib
    from looper.agents.tiers.planner_protocol import REPLY_CONTRACT, reply_contract
    assert reply_contract() == REPLY_CONTRACT
    assert "adjudications" not in reply_contract(review_panel=True)
    assert "adjudications" in reply_contract(review_panel=True, adjudication=True)
    panel = reply_contract(workflow_roles=True, review_panel=True)
    assert 'fixed workflow work role' in panel and 'review_roles", fixed workflow' not in panel
    assert hashlib.sha256(reply_contract().encode()).hexdigest() == 'b70ffeae8558158dbf5c765d6b8ffe9fe8ef425d7aede3a1e9ce6f5af5adb588'


def test_invalid_work_decision_cannot_bypass_panel(planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch)
    claude = planner_claude(plan(step("s1", review=True)), "invalid", "still invalid", decide("accept"))

    async def scenario(h):
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        assert h.planner._plan.steps[0].status == "completed"
        for _ in MEMBERS:
            await h.release(REVIEW_PASS)
        assert (await h.finished()).payload["status"] == "completed"
    drive(claude, held_reviews, scenario)


def test_user_restart_discards_old_passes_when_work_runs_again(planner_claude, agent_db, monkeypatch):
    from looper.agents.message import MessageEnvelope, TASK_ROUTER_ADDRESS
    from looper.agents.message import RestartMode
    configure(agent_db, monkeypatch)
    claude = planner_claude(plan(step("s1", review=True)), decide("review"),
                            decide("fail", reason="Try again later"), chat("Restarting"),
                            decide("review"), decide("accept"))

    async def scenario(h):
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        await h.release(REVIEW_PASS)
        await h.release(BLOCK)
        await h.finished()
        saved = h.planner._plan

        async def load(*args):
            h.planner._plan = saved
        monkeypatch.setattr(h.planner, "_load_plan", load)
        h.statuses.clear()
        await h.bus.publish(MessageEnvelope.create(source=TASK_ROUTER_ADDRESS, target=h.planner.agent_address,
            message_type=MessageType.TASK_REQUEST, payload={"restart": RestartMode.CONTINUE, "ended": "failed"}))
        await h.until(lambda: len(h.reviews) == 6)
        assert all(s.retries == 1 for s in saved.steps if s.reviews)
        assert all(s.result is None for s in saved.steps if s.reviews)
        for _ in MEMBERS:
            await h.release(REVIEW_PASS)
        assert (await h.finished()).payload["status"] == "completed"
    drive(claude, held_reviews, scenario)


def test_isolated_review_keeps_its_workspace_without_blocking_accepted_downstream(
    planner_claude, agent_db, monkeypatch,
):
    from pathlib import Path
    from tests.test_planner_workspaces import git, init_repo
    configure(agent_db, monkeypatch)
    agent_db.get_task_project.return_value["step_workspaces"] = "worktree"
    claude = planner_claude(plan(step("s1", review=True), step("s2"), step("s3", "s1")),
                            decide("review"), decide("accept"), decide("accept"), decide("accept"))

    def outcome(envelope):
        if envelope.message_type == MessageType.VERIFY_REQUEST:
            return None
        directory = Path(envelope.payload["context"]["work_dir"])
        if envelope.payload["step_key"] == "s1":
            (directory / "result.txt").write_text("review this output\n")
            return {**WORK_OK, "head": git(directory, "rev-parse", "HEAD")}
        return WORK_OK

    async def scenario(h):
        init_repo(h.work_dir)
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        reviewed = Path(h.reviews[0].payload["context"]["work_dir"])
        frozen = h.reviews[0].payload["head"]
        assert frozen == git(reviewed, "rev-parse", "HEAD")
        assert reviewed != h.work_dir
        await h.release(REVIEW_PASS)
        await h.release(REVIEW_PASS)
        await h.until(lambda: any(e.payload["step_key"] == "s3" for e in h.work_items))
        assert reviewed.is_dir() and git(reviewed, "rev-parse", "HEAD") == frozen
        assert (h.work_dir / "result.txt").read_text() == "review this output\n"
        await h.release(REVIEW_PASS)
        await h.finished()
    drive(claude, outcome, scenario)


@pytest.mark.parametrize("mode,commit_output", [("shared", True), ("shared", False), ("worktree", False)])
def test_panel_reviews_use_independent_commits_while_another_step_commits(
    planner_claude, agent_db, monkeypatch, tmp_path, mode, commit_output,
):
    from pathlib import Path
    from fake_claude import fake_claude
    from looper.agents.tiers.worker import WorkerConfig, WorkerPool
    from tests.test_review_panel_workspaces import git
    from tests.test_planner_workspaces import init_repo

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    configure(agent_db, monkeypatch)
    agent_db.get_task_project.return_value["step_workspaces"] = mode
    claude = planner_claude(plan(step("s1", review=True), step("s2")),
                            decide("review"), decide("accept"), decide("accept"))

    def outcome(envelope):
        if envelope.message_type == MessageType.WORK_ITEM and envelope.payload["step_key"] == "s1":
            directory = Path(envelope.payload["context"]["work_dir"])
            (directory / "result.txt").write_text("reviewed output\n")
            if commit_output:
                git(directory, "add", "result.txt")
                git(directory, "commit", "-qm", "Deliver result")
            return {**WORK_OK, "head": git(directory, "rev-parse", "HEAD")}
        return None

    async def scenario(h):
        init_repo(h.work_dir)
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        if mode == "shared" and not commit_output:
            assert "result.txt" in git(h.work_dir, "status", "--porcelain")
            assert git(h.work_dir, "diff", "--cached") == ""
            assert git(h.work_dir, "rev-parse", "HEAD") != h.reviews[0].payload["head"]
        other = next(e for e in h.work_items if e.payload["step_key"] == "s2")
        other_dir = other.payload["context"]["work_dir"]
        # The reviewer runs a real check; an independent worker commits during that run.
        executable = fake_claude(tmp_path / "review", "import json, subprocess\nfrom pathlib import Path\n"
            "before = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()\n"
            "assert Path('result.txt').read_text() == 'reviewed output\\n'\n"
            "assert not Path('review-output.txt').exists()\n"
            "Path('review-output.txt').write_text('test output')\n"
            f"subprocess.run(['git', '-C', {other_dir!r}, '-c', 'user.name=Example', "
            "'-c', 'user.email=dev@example.org', 'commit', '--allow-empty', '-qm', 'Other step'], check=True)\n"
            "after = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'result': json.dumps("
            "{'passed': before == after, 'findings': []})}), flush=True)\n")
        pool = WorkerPool(config=WorkerConfig(claude_bin=executable), runtime_root=tmp_path / "runtime")
        directories = [Path(e.payload["context"]["work_dir"]) for e in h.reviews]
        for envelope in h.reviews:
            result = await pool._execute(envelope, is_verify=True)
            assert result["passed"] is True, result
            assert "failure" not in result
            assert git(envelope.payload["context"]["work_dir"], "rev-parse", "HEAD") == envelope.payload["head"]
            h.held.remove(envelope)
            await h.answer(envelope, result)
        assert len(set(directories)) == 3
        assert all(d != h.work_dir and str(d) != other_dir for d in directories)
        assert not (h.work_dir / "review-output.txt").exists()
        h.held.remove(other)
        await h.answer(other, WORK_OK)
        assert (await h.finished()).payload["status"] == "completed"
        assert all(s.status == "verified" for s in h.planner._plan.steps)
        assert all(not directory.exists() for directory in directories)
    drive(claude, outcome, scenario)


def test_panel_retry_discards_old_snapshots_and_reviews_the_new_output(planner_claude, agent_db, monkeypatch):
    from pathlib import Path
    from tests.test_review_panel_workspaces import git
    from tests.test_planner_workspaces import init_repo

    configure(agent_db, monkeypatch)
    claude = planner_claude(plan(step("s1", review=True)), decide("review"),
                            decide("retry", feedback="Fix the output"), decide("review"), decide("accept"))

    def outcome(envelope):
        if envelope.message_type == MessageType.WORK_ITEM:
            directory = Path(envelope.payload["context"]["work_dir"])
            (directory / "output.txt").write_text(str(envelope.payload["attempt"]))
            return {**WORK_OK, "head": git(directory, "rev-parse", "HEAD")}
        return None

    async def scenario(h):
        init_repo(h.work_dir)
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        old = [Path(e.payload["context"]["work_dir"]) for e in h.reviews]
        old_head = h.reviews[0].payload["head"]
        await h.release(BLOCK)
        await h.release(REVIEW_PASS)
        await h.until(lambda: len(h.reviews) == 6)
        assert all(not directory.exists() for directory in old)
        for envelope in h.reviews[3:]:
            directory = Path(envelope.payload["context"]["work_dir"])
            assert directory not in old
            assert (directory / "output.txt").read_text() == "2"
            assert envelope.payload["head"] == git(directory, "rev-parse", "HEAD") != old_head
        for _ in MEMBERS:
            await h.release(REVIEW_PASS)
        assert (await h.finished()).payload["status"] == "completed"
    drive(claude, outcome, scenario)


def test_review_chosen_after_work_still_uses_the_fixed_panel(planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch)
    claude = planner_claude(plan(step("s1")), decide("review"), decide("accept"))

    async def scenario(h):
        await h.start()
        await h.until(lambda: len(h.reviews) == 3)
        assert {e.payload["task_role"]["id"] for e in h.reviews} == set(MEMBERS)
        for _ in MEMBERS:
            await h.release(REVIEW_PASS)
        await h.finished()
    drive(claude, held_reviews, scenario)


@pytest.mark.parametrize("change", [
    {"member": "missing"}, {"finding": 1}, {"level": "blocking"}, {"reason": "a" * 501},
])
def test_adjudication_must_address_current_blocking_with_a_bounded_reason(tmp_path, change):
    from looper.agents.tiers.planner import PlannerAgent, PlanStep, TaskPlan
    from looper.agents.tiers.planner_protocol import DecisionContext, ProtocolError, ResultKind
    agent = PlannerAgent(task_id="example", runtime_root=tmp_path)
    agent._workflow = {"adjudication": True}
    target = PlanStep("work", 0, "Work", accept_when={"step": "seat", "on": "passed"})
    seat = PlanStep("seat", 1, "Review", reviews="work", role="review_a", status="verified", result=BLOCK)
    task = TaskPlan("plan", "goal", "Work", "", steps=[target, seat])
    context = DecisionContext(target.label, ResultKind.REVIEW, False, True, 0)
    proposal = {"member": "review_a", "finding": 0, "level": "suggestion", "reason": "Already handled", **change}
    with pytest.raises(ProtocolError):
        agent._panel_decision(decide("accept", adjudications=[proposal]), task, target, context, task.view())
    assert seat.result == BLOCK  # invalid proposals never partially change live verdicts


def test_review_all_excludes_the_workflow_verification_role(planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch, review_all=True, verification={"role": "verify_staging", "max_rounds": 2},
              roles=[*PANEL["roles"], {"key": "verify_staging", "kind": "work", "role_prompt": "Verify the change."}])
    rows = task_roles.current.return_value
    rows.append({**rows[0], "role_key": "verify_staging", "kind": "work", "role_prompt": "Verify the change."})
    monkeypatch.setattr(task_roles, "get", AsyncMock(side_effect=lambda _, key, *args: next(
        row for row in rows if row["role_key"] == key)))
    claude = planner_claude(plan(step("work"), {**step("s1", "work", review=True), "role": "verify_staging"}),
                            decide("review"), decide("accept"), decide("accept"))
    h = drive(claude, lambda envelope: REVIEW_PASS if envelope.message_type == MessageType.VERIFY_REQUEST else WORK_OK)
    assert h.final().payload["status"] == "completed"
    work, verification = h.planner._plan.steps[:2]
    assert len(h.reviews) == len(MEMBERS)
    assert all(envelope.payload["step_key"] == work.label for envelope in h.reviews)
    assert verification.accept_when is None
    assert h.work_items[-1].payload["verification"] is True
