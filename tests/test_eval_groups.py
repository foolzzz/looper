"""Grouped prompt-eval reports pool counts and preserve incomplete evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evals.prompt_regression import __main__ as cli
from evals.prompt_regression import compare, runner


def _row(**values: Any) -> dict[str, Any]:
    return {"id": "task", "rep": 1, "outcome": "pass", "success": True,
            "first_pass_steps": 1, "accepted_steps": 1, "retries": 0, "cost_usd": 0.01,
            "duration_s": 2.0, "planner_turns": {"plan": 1, "decision": 1}, "roles_defined": 2,
            "role_runs": 1, **values}


def _run(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"schema": runner.SCHEMA, "mode": "fake", "tasks": list(rows)}


def _cells(table: str, label: str) -> dict[str, str]:
    lines = table.splitlines()
    header = [cell.strip() for cell in lines[0].split("|")[1:-1]]
    row = next(line for line in lines if line.startswith(f"| {label} |"))
    return dict(zip(header, [cell.strip() for cell in row.split("|")[1:-1]]))


def test_groups_pool_steps_and_sum_started_cases_including_error_costs() -> None:
    error = _row(rep=3, outcome="error", success=False, error_class="timeout", cost_usd=0.2,
                 first_pass_steps=90, accepted_steps=100, retries=2, role_runs=0)
    table = compare.groups_markdown({"on": _run(_row(), _row(rep=2, first_pass_steps=0, accepted_steps=9,
                                                           retries=3), error,
                                              {"id": "task", "rep": 4, "skipped": "cap"})})
    cells = _cells(table, "on")
    assert cells == {"group": "on", "success": "2/2 (100%, 34–100%)",
                     "first-pass rate": "1/10 (10%, 2–40%)", "rework count": "5.0",
                     "cost (USD)": "0.2200", "duration (s)": "6.0", "Planner turns": "6.0",
                     "role adoption (cases)": "2/3", "roles defined": "6.0", "role runs": "2.0",
                     "errors": "1", "skipped": "1"}


def test_groups_distinguish_missing_zero_and_partial_metrics() -> None:
    table = compare.groups_markdown({"missing": _run({"id": "old", "outcome": "pass"}),
                                     "partial": _run(_row(), {"id": "old", "outcome": "pass"}),
                                     "zero": _run(_row(first_pass_steps=0, accepted_steps=0, retries=0,
                                                       role_runs=0, roles_defined=0, planner_turns={})),
                                     "empty": _run()})
    for name in ("missing", "partial", "empty"):
        cells = _cells(table, name)
        assert all(cells[key] == "–" for key in ("first-pass rate", "rework count", "cost (USD)",
                                                  "duration (s)", "Planner turns", "role adoption (cases)"))
    assert _cells(table, "zero")["first-pass rate"] == "0/0"
    assert _cells(table, "zero")["role adoption (cases)"] == "0/1"
    assert _cells(table, "zero")["Planner turns"] == "0.0"
    assert "different task/rep sets" in table


def test_groups_ignore_only_explicit_flow_failures_and_do_not_mutate_results() -> None:
    run = _run(_row(outcome="fail", success=False, completion_checks_passed=True,
                    problems=["roles_defined 0 < 1"]),
               _row(rep=2, outcome="fail", success=False, completion_checks_passed=False,
                    problems=["output.txt is missing"]),
               _row(rep=3, outcome="error", error_class="harness", completion_checks_passed=True),
               {"id": "task", "rep": 4, "skipped": "cap", "completion_checks_passed": True})
    before = json.dumps(run)
    assert _cells(compare.groups_markdown({"off": run}), "off")["success"] == "0/2 (0%, 0–66%)"
    judged = compare.groups_markdown({"off": run}, ignore_flow_assertions=True)
    assert _cells(judged, "off")["success"] == "1/2 (50%, 9–91%)"
    assert _cells(judged, "off")["errors"] == "1"
    assert _cells(judged, "off")["skipped"] == "1"
    assert json.dumps(run) == before


def test_groups_require_evidence_for_old_failures_but_accept_old_passes() -> None:
    assert "1/1" in compare.groups_markdown({"old": _run(_row())}, ignore_flow_assertions=True)
    with pytest.raises(runner.EvalError, match="no completion/check evidence"):
        compare.groups_markdown({"old": _run(_row(outcome="fail", status="completed",
                                                  problems=["roles_defined 0 < 1"]))},
                                ignore_flow_assertions=True)
    with pytest.raises(runner.EvalError, match="not Manager dialogs"):
        compare.groups_markdown({"dialogs": {"dialogs": []}})


def test_groups_cli_reads_labelled_synthetic_result_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths = []
    for label in ("roles-on", "roles-off", "worker-profile"):
        path = tmp_path / f"{label}.json"
        path.write_text(json.dumps(_run(_row(outcome="fail", success=False, completion_checks_passed=True))),
                        encoding="utf-8")
        paths.append(f"{label}={path}")
    assert cli.main(["groups", *paths, "--ignore-flow-assertions"]) == 0
    table = capsys.readouterr().out
    assert all(_cells(table, label)["success"] == "1/1 (100%, 21–100%)"
               for label in ("roles-on", "roles-off", "worker-profile"))


@pytest.mark.parametrize("entry", ["unlabelled.json", "=file.json", "label="])
def test_groups_cli_rejects_unlabelled_files(entry, capsys):
    assert cli.main(["groups", entry]) == 2
    assert "unique LABEL=RUN.json" in capsys.readouterr().err


def test_groups_cli_rejects_duplicate_labels(tmp_path, capsys):
    path = tmp_path / "run.json"
    path.write_text(json.dumps(_run(_row())))
    assert cli.main(["groups", f"same={path}", f"same={path}"]) == 2
    assert "unique LABEL=RUN.json" in capsys.readouterr().err
