"""Claude Code API failures retain a safe reason through the agent tiers."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from fake_claude import fake_claude
from looper.agents.runtimes import CLAUDE_CODE
from looper.agents.tiers.worker import WorkerConfig, WorkerPool, judge_result
from tests.test_agent_planner_worker import _work_item
from tests.test_manager_reply import _ask
from tests.test_planner_agent import WORK_OK, always, drive


def _result(reason=None, **fields):
    return {"type": "result", "subtype": "success", "is_error": True, "result": reason, **fields}


def _body(event):
    return (
        "import json, sys\n"
        "message = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'test-session'}), flush=True)\n"
        f"event = json.loads({json.dumps(event)!r})\n"
        "event.update(session_id='test-session', user_message_uuid=message.get('uuid'))\n"
        "print(json.dumps(event), flush=True)\n"
    )


@pytest.mark.parametrize(("reason", "expected"), [
    ("OAuth token has expired. Please run /login.", ": OAuth token has expired. Please run /login."),
    ("You've hit your limit · resets at 5pm", ": You've hit your limit · resets at 5pm"),
    (" API Error:\n 503\tService unavailable ", ": API Error: 503 Service unavailable"),
    ("x" * 600, ": " + "x" * 300),
    ("Invalid Bearer test-token and sk-test-secret", ": Invalid [redacted] and [redacted]"),
    (None, " (success)"),
    (" \n\t", " (success)"),
])
def test_planner_failure_reason_reaches_record_and_task(agent_db, tmp_path, reason, expected):
    claude = SimpleNamespace(bin=fake_claude(tmp_path / "claude", _body(_result(reason))))

    async def finished(task):
        await task.start()
        await task.until(task.final, timeout=45)

    task = drive(claude, always(WORK_OK), finished, timeout=60)
    error = "Claude Code turn did not succeed" + expected
    assert task.final().payload["status"] == "failed"
    assert "Planning failed: " + error in task.final().payload["summary"]
    assert agent_db.finish_record.await_args_list[0].kwargs["error"] == error
    assert agent_db.finish_record.await_args_list[0].kwargs["status"] == "failed"
    assert not task.dispatched


def test_manager_login_failure_reaches_record_and_operator_reply(agent_db):
    reason = "Not logged in. Please run /login."
    reply = _ask(_body(_result(reason)))
    error = "Claude Code turn did not succeed: " + reason
    assert error in reply["text"]
    assert agent_db.finish_record.await_args_list[0].kwargs["error"] == error


@pytest.mark.parametrize("is_verify", [False, True])
def test_worker_and_reviewer_failure_reason_reaches_record(agent_db, tmp_path, is_verify):
    reason = "API Error: 401 Invalid Bearer test-token"
    executable = fake_claude(tmp_path / "claude", _body(_result(reason)))

    async def run():
        pool = WorkerPool(config=WorkerConfig(claude_bin=executable), runtime_root=tmp_path)
        return await asyncio.wait_for(pool._execute(_work_item(tmp_path), is_verify=is_verify), 15)

    outcome = asyncio.run(run())
    error = "Claude Code turn did not succeed: API Error: 401 Invalid [redacted]"
    assert outcome["summary"] == error
    if is_verify:
        assert outcome["passed"] is False
    else:
        assert outcome["status"] == "failed" and outcome["error"] == error
    assert agent_db.finish_record.await_args_list[0].kwargs["error"] == error


def test_other_runtime_failure_detail_keeps_its_wording_and_bound(tmp_path):
    outcome = judge_result(_result(), work_dir=tmp_path, is_verify=False, runtime_label="OMP",
                           runtime_type="omp", failure_detail="x" * 600)
    assert outcome["error"] == ("OMP run did not succeed: " + "x" * 600)[:500]


@pytest.mark.parametrize(("event", "expected"), [
    (None, None),
    (_result({"message": "malformed"}), None),
    (_result("Successful answer", is_error=False), None),
    (_result("No permission", status_code=403), "HTTP 403 No permission"),
    (_result("No permission", status=403), "HTTP 403 No permission"),
    (_result(status_code=401), "HTTP 401"),
    (_result("Failed", status_code=True), "Failed"),
    (_result("Failed", status_code="Bearer test-token"), "Failed"),
    (_result("x" * 280 + " sk-" + "secret" * 100), "x" * 280 + " [redacted]"),
    (_result('api_key="test-key" rejected'), 'api_key=[redacted] rejected'),
    (_result("test-key rejected"), "[redacted] rejected"),
])
def test_failure_detail_boundaries(event, expected):
    from tests.test_claude_cli import _priced_launch

    detail = CLAUDE_CODE.failure_detail(event, launch=_priced_launch(), command=[])
    assert detail == expected
    assert detail is None or len(detail) <= 300
