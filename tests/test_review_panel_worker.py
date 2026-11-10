"""Review verdict normalization and runtime isolation on the worker entry point."""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess

import pytest

from fake_claude import fake_claude
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.system_prompt import REVIEW_CONTRACT, review_contract
from looper.agents.tiers.worker import WorkerConfig, WorkerPool, judge_result


def verdict(tmp_path, findings, passed=True, *, severity=True):
    event = {"subtype": "success", "result": json.dumps({"passed": passed, "findings": findings})}
    return judge_result(event, work_dir=tmp_path, is_verify=True, severity=severity)


@pytest.mark.parametrize("finding", [
    {"severity": "blocking", "where": "app.py:3", "text": "Wrong result"},
    {"severity": "unknown", "where": "app.py:3", "text": "Wrong result"},
    "Wrong result",
])
def test_blocking_findings_override_passed(tmp_path, finding):
    result = verdict(tmp_path, [finding])
    assert result["passed"] is False and result["inconsistent"] is True
    assert result["findings"][0]["severity"] == "blocking"
    assert result["findings"][0]["text"] == "Wrong result"


def test_suggestions_do_not_fail_a_review(tmp_path):
    finding = {"severity": "suggestion", "where": "app.py:3", "text": "Clarify the name"}
    result = verdict(tmp_path, [finding])
    assert result["passed"] is True and "inconsistent" not in result
    assert result["findings"] == [finding]


def test_explicit_failed_verdict_stays_failed_without_blocking(tmp_path):
    assert verdict(tmp_path, [], passed=False)["passed"] is False


def test_legacy_verdict_and_contract_are_unchanged(tmp_path):
    assert review_contract(False) == REVIEW_CONTRACT
    assert hashlib.sha256(REVIEW_CONTRACT.encode()).hexdigest() == "4605220c239c54b4fc3321f2423720cbea8e6aa4102c6493ebd3f1eabe1ae3e4"
    result = verdict(tmp_path, ["A legacy finding"], severity=False)
    assert result["passed"] is True and result["findings"] == ["A legacy finding"]
    assert "inconsistent" not in result


def run(tmp_path, body, *, verify=False, **extra):
    executable = fake_claude(tmp_path / "fake-review", "import json, sys, subprocess\n" + body)
    pool = WorkerPool(config=WorkerConfig(claude_bin=executable), runtime_root=tmp_path / "runtime")
    envelope = MessageEnvelope.create(
        source=planner_address("task-1"), target=worker_address("review_a" if verify else "dev"),
        message_type=MessageType.VERIFY_REQUEST if verify else MessageType.WORK_ITEM,
        payload={"task_id": "step-1", "context": {"work_dir": str(tmp_path)}, "instruction": "Check result",
                 **extra},
    )
    return asyncio.run(pool._execute(envelope, is_verify=verify))


REPORT = "print(json.dumps({'type':'result','subtype':'success','result':json.dumps(REPORT)}), flush=True)\n"


def test_same_runtime_never_starts_reviewer(tmp_path, agent_db):
    result = run(tmp_path, "open('started', 'w').write('yes')\n", verify=True, severity=True,
                 worker_runtime_id="rt-claude-code")
    assert not (tmp_path / "started").exists()
    assert result["passed"] is False and result["refused"] == "same_runtime"
    assert result["findings"][0]["text"] == "same_runtime_as_worker"


def git(tmp_path, *args):
    return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True,
                          check=True, timeout=10).stdout.strip()


def init_repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "-c", "user.name=Example", "-c", "user.email=dev@example.org", "commit", "--allow-empty",
        "-qm", "Initial")
    return git(tmp_path, "rev-parse", "HEAD")


def test_work_result_records_head_and_runtime(tmp_path, agent_db):
    head = init_repo(tmp_path)
    result = run(tmp_path, REPORT.replace("REPORT", repr({"status": "completed", "changed_files": {}})))
    assert result["head"] == head and result["runtime_id"] == "rt-claude-code"


def test_work_outside_git_records_null_head(tmp_path, agent_db, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    result = run(tmp_path, REPORT.replace("REPORT", repr({"status": "completed", "changed_files": {}})))
    assert result["head"] is None


def test_head_moved_during_review_fails(tmp_path, agent_db):
    head = init_repo(tmp_path)
    body = ("subprocess.run(['git', '-c', 'user.name=Example', '-c', 'user.email=dev@example.org', "
            "'commit', '--allow-empty', '-qm', 'Moved'], check=True)\n"
            + REPORT.replace("REPORT", repr({"passed": True, "findings": []})))
    result = run(tmp_path, body, verify=True, severity=True, head=head)
    assert result["passed"] is False and result["failure"] == "head_moved"
    assert result["findings"][-1]["text"] == "head_moved"
    assert agent_db.finish_record.await_args.kwargs["outcome"] == "not_passed"


def test_panel_prompt_uses_severity_contract(tmp_path, agent_db):
    body = ("path = sys.argv[sys.argv.index('--append-system-prompt-file') + 1]\n"
            "open('seen-prompt', 'w').write(open(path).read())\n"
            + REPORT.replace("REPORT", repr({"passed": True, "findings": []})))
    result = run(tmp_path, body, verify=True, severity=True)
    assert result["passed"] is True
    prompt = (tmp_path / "seen-prompt").read_text()
    assert prompt.endswith(review_contract(True)) and not prompt.endswith(REVIEW_CONTRACT)
    assert "passed is true only when no finding is blocking" in prompt


def test_failed_work_still_records_head_and_runtime(tmp_path, agent_db):
    head = init_repo(tmp_path)
    result = run(tmp_path, "sys.exit(1)\n")
    assert result["status"] == "failed" and result["head"] == head
    assert result["runtime_id"] == "rt-claude-code"


def test_failed_review_run_has_blocking_finding(tmp_path, agent_db):
    result = run(tmp_path, "sys.exit(1)\n", verify=True, severity=True)
    assert result["passed"] is False and result["findings"][0]["severity"] == "blocking"


def test_early_prepare_failure_is_a_structured_blocking_verdict(tmp_path, agent_db):
    result = run(tmp_path, "raise AssertionError('must not start')\n", verify=True, severity=True, context={})
    assert result["passed"] is False
    assert result["findings"][0]["severity"] == "blocking"
    assert "work directory" in result["findings"][0]["text"]


def test_unexpected_review_failure_publishes_a_structured_verdict(tmp_path, agent_db, monkeypatch):
    from unittest.mock import AsyncMock
    from looper.agents.bus import MessageBus

    pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
    bus = pool._bus = MessageBus(runtime_root=tmp_path)
    monkeypatch.setattr(pool, "_execute", AsyncMock(side_effect=RuntimeError("review unavailable")))
    envelope = MessageEnvelope.create(source=planner_address("task-1"), target=worker_address("review_a"),
        message_type=MessageType.VERIFY_REQUEST, payload={"task_id": "step-1", "severity": True})
    asyncio.run(pool._run_worker(envelope))
    result = bus.get_queue("planner-task-1").get_nowait().payload
    assert result["passed"] is False
    assert result["findings"] == [{"severity": "blocking", "where": "unknown", "text": "review unavailable"}]


@pytest.mark.parametrize("verify,expected", [(False, '14f6bc1407fe9fd8f27740be743b7ea5855773ab8c17d09b8d93c07a10ebb868'), (True, 'f996d7bf88438205171f7e9e5af7c80208fda5bce0271deb0235b5147216c421')])
def test_default_work_and_review_system_prompts_match_released_bytes(verify, expected):
    from looper.agents.agent_settings import Harness, Layer
    from looper.agents.system_prompt import WORK_CONTRACT, build_system_prompt
    payload = {"requirement": "Build the project", "step_key": "s1", "instruction": "Implement the change",
               "acceptance_criteria": ["Tests pass"]}
    prompt = build_system_prompt(harness=Harness("Work carefully.", Layer.BUILTIN),
        role_id="reviewer" if verify else "worker", role="Use the project rules.", recommended=False,
        task_kind="review" if verify else "work", task_id="task-baseline", step_id="s1",
        task=WorkerPool._task_section(payload, "", is_verify=verify),
        contract_name="REVIEW_CONTRACT" if verify else "WORK_CONTRACT",
        contract=review_contract(False) if verify else WORK_CONTRACT)
    assert prompt.sha256 == expected
