"""Task results report measured states without forwarding commands or output."""
import asyncio
from collections import Counter
from unittest.mock import AsyncMock

import pytest

from looper.agents import acceptance_result as result
from looper.agents.acceptance_checks import CommandCheck
from looper.agents.tiers.planner import PlanStep


def step(key="s1", **fields):
    return PlanStep("db-" + key, 0, "Measure", key=key,
                    acceptance_criteria=["works", "readable"], **fields)


def check(key="c1", criterion=1, required=True):
    return CommandCheck(id=key, criterion=criterion, command="PRIVATE_COMMAND",
                 required=required)


def row(state="passed", **fields):
    return dict(step_id="db-s1", check_id="c1", attempt=1, run=1, state=state,
                head_sha="1a2b3c4d5e6f7890", guarded=True, output_tail="PRIVATE_OUTPUT",
                artifacts=[{"name": "PRIVATE_ARTIFACT"}], **fields)


def summarize(monkeypatch, steps, rows):
    read = AsyncMock(return_value=rows)
    monkeypatch.setattr(result.step_check_runs, "list_runs", read)
    answer = asyncio.run(result.summarize("task-fixture", steps))
    read.assert_awaited_once_with("task-fixture")
    return answer


@pytest.mark.parametrize("state,fields,shown", [
    ("passed", {}, "passed at 1a2b3c4d5e6f"),
    ("failed", {}, "failed at 1a2b3c4d5e6f"),
    ("passed", {"protected_changed": ["PRIVATE_PATH"]}, "failed at 1a2b3c4d5e6f"),
    ("error", {"error_kind": "timeout"}, "error (timeout) at 1a2b3c4d5e6f"),
    ("stale", {}, "stale at 1a2b3c4d5e6f"),
    ("running", {}, "not run"),
])
def test_measured_states_and_no_sensitive_details(monkeypatch, state, fields, shown):
    answer = summarize(monkeypatch, [step(checks=[check()])], [row(state, **fields)])
    assert answer.section == f"## Evidence\n- s1 c1 (criterion 1): {shown}\n1 criteria have no check."
    assert "PRIVATE" not in answer.section
    assert answer.counts.line().startswith("Checks: 1 ")


@pytest.mark.parametrize("guarded,head", [(False, "abcdefabcdef9999"), (True, None)])
def test_advisory_and_report_only(monkeypatch, guarded, head):
    measured = {**row("failed"), "guarded": guarded, "head_sha": head}
    answer = summarize(monkeypatch, [step(checks=[check(required=False)])], [measured])
    assert "(criterion 1, advisory): failed, unguarded (report_only)" in answer.section
    assert (" at " in answer.section) == bool(head)
    assert answer.counts.line() == "Checks: 1 advisory failed"


def test_current_attempt_and_defined_checks_only_and_uncovered_criteria(monkeypatch):
    steps = [step(checks=[check(), check("c2", 2)]), step("s2", retries=1, checks=[check()]),
             step("s3"), step("skip", status="skipped", checks=[check()]),
             step("gate", gate=object()), step("wait", wait=object())]
    rows = [row(), {**row("failed"), "run": 2}, {**row(), "check_id": "removed"},
            {**row(), "step_id": "db-s2"}, {**row(), "step_id": "db-skip"}]
    answer = summarize(monkeypatch, steps, rows)
    assert "s1 c1 (criterion 1): failed" in answer.section
    assert "s1 c2 (criterion 2): not run" in answer.section
    assert "s2 c1 (criterion 1): not run" in answer.section
    assert answer.section.endswith("3 criteria have no check.")
    assert "removed" not in answer.section and "skip" not in answer.section
    assert answer.counts.line() == "Checks: 1 failed, 2 not run"


def test_all_criteria_bound_and_no_kept_evidence(monkeypatch):
    current = step(checks=[check(), check("c2", 2)])
    answer = summarize(monkeypatch, [current], [row()])
    assert "criteria have no check" not in answer.section
    for rows in ([], [{**row(), "attempt": 0}], [{**row(), "check_id": "deleted"}]):
        assert summarize(monkeypatch, [current], rows) is None


def test_count_order_omission_and_advisory_passes():
    counts = Counter({(result.ResultState.PASSED, True): 5, (result.ResultState.PASSED, False): 1,
                      (result.ResultState.FAILED, True): 1, (result.ResultState.ERROR, True): 2,
                      (result.ResultState.STALE, True): 3, (result.ResultState.NOT_RUN, True): 4,
                      (result.ResultState.FAILED, False): 1, (result.ResultState.ERROR, False): 2,
                      (result.ResultState.STALE, False): 3, (result.ResultState.NOT_RUN, False): 4})
    assert result.CheckCounts(counts).line() == (
        "Checks: 6 passed, 1 failed, 1 advisory failed, 2 error, 2 advisory error, "
        "3 stale, 3 advisory stale, 4 not run, 4 advisory not run")
    assert result.CheckCounts({}).line() == ""
    assert result.CheckCounts({(result.ResultState.PASSED, True): 6,
        (result.ResultState.FAILED, True): 1, (result.ResultState.FAILED, False): 1,
        (result.ResultState.NOT_RUN, True): 1}).line() == (
            "Checks: 6 passed, 1 failed, 1 advisory failed, 1 not run")


@pytest.mark.parametrize("value", [None, {}, [], "bad", {"section": 1, "line": "x"},
                                    {"section": "x", "line": None}])
def test_malformed_payload_is_absent(value):
    assert result.ResultEvidence.from_payload(value) is None


def test_payload_roundtrip_without_parsing_stored_text():
    payload = {"section": "## Evidence\n- s1 c1 (criterion 1): passed", "line": "Checks: 1 passed"}
    answer = result.ResultEvidence.from_payload(payload)
    assert answer.payload() == payload
    assert answer.counts.line() == payload["line"]
