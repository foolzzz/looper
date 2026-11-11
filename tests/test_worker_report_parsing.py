"""Report parsing at the worker/reviewer execution and run-record boundary."""
from __future__ import annotations

import asyncio
import json

import pytest

from fake_claude import fake_claude
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.tiers.worker import WorkerConfig, WorkerPool


def run_report(tmp_path, text, verify):
    executable = fake_claude(tmp_path / "report-runtime", "import json\nprint(json.dumps("
        + repr({"type": "result", "subtype": "success", "result": text}) + "), flush=True)\n")
    pool = WorkerPool(config=WorkerConfig(claude_bin=executable), runtime_root=tmp_path / "runtime")
    envelope = MessageEnvelope.create(
        source=planner_address("task"), target=worker_address("reviewer" if verify else "worker"),
        message_type=MessageType.VERIFY_REQUEST if verify else MessageType.WORK_ITEM,
        payload={"task_id": "step", "instruction": "Check the result", "severity": verify,
                 "context": {"work_dir": str(tmp_path)}},
    )
    return asyncio.run(pool._execute(envelope, is_verify=verify))


@pytest.mark.parametrize("verify", [False, True])
@pytest.mark.parametrize("learning,expected", [
    (r"use [^\W_]", r"use [^\W_]"),
    (r"literal \u12xz and \u123", r"literal \u12xz and \u123"),
    (r'\W \" \\ \/ \b \f \n \r \t \u0041 \\\W', '\\W " \\ / \b \f \n \r \t A \\\\W'),
])
def test_stray_escapes_preserve_text_and_record_repair(tmp_path, agent_db, verify, learning, expected):
    header = '"passed": true' if verify else '"status": "completed", "changed_files": {}'
    text = ('```json\n{' + header + ', "learnings": ["' + learning
            + '"], "summary": "' + learning + '", "details": {"passed": false, "status": "failed"}}\n```')
    result = run_report(tmp_path, text, verify)
    assert result.get("passed") is True if verify else result["status"] == "completed"
    assert result["summary"] == expected
    assert result["learnings"] == [" ".join(expected.split())]
    assert agent_db.finish_record.await_args.kwargs["outcome"] == ("passed" if verify else "completed")
    assert any(call.args[1].get("report_repaired") is True for call in agent_db.add_metadata.await_args_list)


@pytest.mark.parametrize("verify", [False, True])
@pytest.mark.parametrize("body", [
    '{"KEY": VALUE,\n "summary": "broken",}',
    r'{"KEY": VALUE, "summary": "\W",}',
    '{"KEY" VALUE}',
])
def test_other_syntax_errors_name_the_parse_failure(tmp_path, agent_db, verify, body):
    key, value = ("passed", "true") if verify else ("status", '"completed"')
    text = "```json\n" + body.replace("KEY", key).replace("VALUE", value) + "\n```"
    with pytest.raises(json.JSONDecodeError) as caught:
        json.JSONDecoder().raw_decode(text, text.index("{"))
    error = caught.value
    subject = "Reviewer ended with a JSON verdict" if verify else "Worker ended with a JSON status report"
    expected = f"{subject} that does not parse: {error.msg} (line {error.lineno} column {error.colno})"
    result = run_report(tmp_path, text, verify)
    assert result.get("passed") is False if verify else result["status"] == "failed"
    assert result["summary"] == expected
    assert agent_db.finish_record.await_args.kwargs["outcome"] == "contract"
    assert not any(call.args[1].get("report_repaired") for call in agent_db.add_metadata.await_args_list)


@pytest.mark.parametrize("verify", [False, True])
@pytest.mark.parametrize("text", ["All done.", '```json\n{"summary": "All done."}\n```'])
def test_missing_report_keeps_the_existing_failure(tmp_path, agent_db, verify, text):
    result = run_report(tmp_path, text, verify)
    expected = ("Reviewer did not end with the JSON verdict" if verify
                else "Worker did not end with the JSON status report")
    assert result["summary"] == expected
    assert agent_db.finish_record.await_args.kwargs["outcome"] == "contract"
    if not verify:
        assert result["final_message"] == text


@pytest.mark.parametrize("verify", [False, True])
@pytest.mark.parametrize("repair_last", [False, True])
def test_repair_marker_belongs_to_the_selected_report(tmp_path, agent_db, verify, repair_last):
    header = '"passed": true' if verify else '"status": "completed"'
    plain = '{' + header + ', "summary": "plain"}'
    repaired = '{' + header + r', "summary": "use \W"}'
    text = "\n".join([plain, repaired] if repair_last else [repaired, plain])
    result = run_report(tmp_path, text, verify)
    assert result["summary"] == (r"use \W" if repair_last else "plain")
    marked = any(call.args[1].get("report_repaired") for call in agent_db.add_metadata.await_args_list)
    assert marked is repair_last
