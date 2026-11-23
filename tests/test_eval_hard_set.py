"""Hard-set checks, attempt columns and the markers a person judges.

Server cases run only when LOOPER_TEST_DATABASE_URL points at a disposable server.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from evals.prompt_regression import __main__ as cli
from evals.prompt_regression import compare, metrics, runner
from evals.prompt_regression.tasks import (
    HARD_TASKS,
    TASKS,
    _HIDDEN_FEATURE_OK,
    _HIDDEN_MAKEFILE,
    _HIDDEN_MAKEFILE_OK,
    _HIDDEN_TEST,
    _MISLEAD_APP,
    _MISLEAD_APP_WRONG,
    _MISLEAD_CONFIG,
    _MISLEAD_CONFIG_OK,
    _MISLEAD_TEST,
    _RETHINK_CART_OK,
    _RETHINK_RECEIPT,
    _RETHINK_RECEIPT_OK,
    _RETHINK_TEST_CART,
    _RETHINK_TEST_RECEIPT,
    check_hidden_precondition,
    check_misleading_error,
    check_rethink_bug,
)
from tests.db_support import DSN

_EN_DASH = "–"
_NEW_COLUMNS = ("first-pass rate", "attempts per passing case", "cost per passing case (USD)")
_ATTEMPTS = {"attempts": 1, "first_pass_steps": 1, "accepted_steps": 1}


def _write(work_dir: Path, files: dict[str, str]) -> None:
    work_dir.mkdir()
    for name, content in files.items():
        (work_dir / name).write_text(content, encoding="utf-8")


def _run(**tasks: dict[str, Any]) -> dict[str, Any]:
    return {"schema": runner.SCHEMA, "mode": "fake", "model": None,
            "git": {"commit": "abcdef1234567", "dirty": False},
            "tasks": [{"id": task_id, **values} for task_id, values in tasks.items()]}


_OK = {"outcome": "pass", "success": True, "steps": 1, "retries": 0, "reviews": 0,
       "contract_violations": {"total": 0}, "planner_modified_files": 0, "cost_usd": 0.02}


def _priced(cost: float, attempts: float = 1) -> dict[str, Any]:
    return {**_OK, **_ATTEMPTS, "attempts": attempts, "cost_usd": cost}


def _column(table: str, task_id: str, header: str) -> str:
    lines = table.splitlines()
    titles = next(line for line in lines if line.startswith("| task |")).split("|")
    index = titles.index(f" {header} ")
    row = next(line for line in lines if line.startswith(f"| {task_id} |"))
    return row.split("|")[index].strip()


def test_hard_set_is_the_three_cases() -> None:
    assert [task.id for task in runner.select_tasks(["hard"])] == [task.id for task in HARD_TASKS]
    assert [task.id for task in HARD_TASKS] == ["hidden_precondition", "rethink_bug", "misleading_error"]
    assert {task.id for task in HARD_TASKS} <= {task.id for task in TASKS}


def test_hidden_precondition_accepts_the_recipe_and_rejects_the_obvious_fix(tmp_path: Path) -> None:
    correct = tmp_path / "correct"
    _write(correct, {"feature.py": _HIDDEN_FEATURE_OK, "test_feature.py": _HIDDEN_TEST,
                     "Makefile": _HIDDEN_MAKEFILE_OK})
    assert check_hidden_precondition(correct) is None
    obvious = tmp_path / "obvious"
    _write(obvious, {"feature.py": _HIDDEN_FEATURE_OK, "test_feature.py": _HIDDEN_TEST,
                     "Makefile": _HIDDEN_MAKEFILE})
    assert check_hidden_precondition(obvious)
    gutted = tmp_path / "gutted"
    _write(gutted, {"feature.py": _HIDDEN_FEATURE_OK, "test_feature.py": "assert True\n",
                    "Makefile": _HIDDEN_MAKEFILE_OK})
    assert check_hidden_precondition(gutted) == "test_feature.py was changed"


def test_rethink_bug_needs_both_modules_and_the_original_tests(tmp_path: Path) -> None:
    correct = tmp_path / "correct"
    _write(correct, {"cart.py": _RETHINK_CART_OK, "receipt.py": _RETHINK_RECEIPT_OK,
                     "test_cart.py": _RETHINK_TEST_CART, "test_receipt.py": _RETHINK_TEST_RECEIPT})
    assert check_rethink_bug(correct) is None
    naive = tmp_path / "naive"
    _write(naive, {"cart.py": _RETHINK_CART_OK, "receipt.py": _RETHINK_RECEIPT,
                   "test_cart.py": _RETHINK_TEST_CART, "test_receipt.py": _RETHINK_TEST_RECEIPT})
    assert check_rethink_bug(naive) == "test_receipt.py exited 1"
    edited = tmp_path / "edited"
    _write(edited, {"cart.py": _RETHINK_CART_OK, "receipt.py": _RETHINK_RECEIPT,
                    "test_cart.py": _RETHINK_TEST_CART, "test_receipt.py": "assert True\n"})
    assert check_rethink_bug(edited) == "test_receipt.py was changed"


def test_misleading_error_rejects_a_hardcoded_answer(tmp_path: Path) -> None:
    correct = tmp_path / "correct"
    _write(correct, {"config.py": _MISLEAD_CONFIG_OK, "app.py": _MISLEAD_APP, "test_app.py": _MISLEAD_TEST})
    assert check_misleading_error(correct) is None
    wrong = tmp_path / "wrong"
    _write(wrong, {"config.py": _MISLEAD_CONFIG, "app.py": _MISLEAD_APP_WRONG, "test_app.py": _MISLEAD_TEST})
    assert check_misleading_error(wrong) == "app.py hardcodes the expected answer"
    untouched = tmp_path / "untouched"
    _write(untouched, {"config.py": _MISLEAD_CONFIG, "app.py": _MISLEAD_APP, "test_app.py": _MISLEAD_TEST})
    assert check_misleading_error(untouched) == "config.py does not set VALUE to ready"


def test_contract_violations_come_from_records_not_logs(tmp_path: Path) -> None:
    activity = [{"kind": "error", "summary": "Planner reply broke the PLAN contract (no json); asking again"}]
    record = {"id": 1, "agent_id": "planner-task-1", "metadata": {}, "status": "completed"}
    common = dict(detail={"status": "completed", "steps": []}, activity=activity, records=[record], events={},
                  changes={}, work_dir=tmp_path, check_problem=None, expect={}, now=0.0)
    logged = metrics.task_metrics(**common)
    assert logged["contract_violations"] == {"planner_rejections": 0, "attempt_contracts": 0, "total": 0}
    record["metadata"] = {"contract_rejections": {"plan": 1}}
    view = {"steps": [{"attempts": [{"run_outcome": "contract"}]}]}
    counted = metrics.task_metrics(**common, attempts_view=view)
    assert counted["contract_violations"] == {"planner_rejections": 1, "attempt_contracts": 1, "total": 2}


def test_attempt_fields_rename_worker_spend_and_skip_absent_counts() -> None:
    view = {
        "summary": {"attempts": 2, "first_pass_steps": 1, "accepted_steps": 1, "failure_kinds": {"contract": 1}},
        "costs": {
            "first": {"worker": {"provider_priced": {"usd": 0.1, "runs": 1, "unknown_runs": 0}},
                      "review": {"provider_priced": {"usd": 0.0, "runs": 0, "unknown_runs": 0}},
                      "total": {"provider_priced": {"usd": 0.1, "runs": 1, "unknown_runs": 0}}},
            "retry": {"worker": {"provider_priced": {"usd": 0.2, "runs": 1, "unknown_runs": 0}}},
            "planning": {"provider_priced": {"usd": 0.3, "runs": 1, "unknown_runs": 0},
                         "equivalent": {"usd": None, "runs": 0, "unknown_runs": 0}},
            "unattributed": {"worker": {"provider_priced": {"usd": 0.0, "runs": 0, "unknown_runs": 0}}},
        },
    }
    fields = metrics.attempt_fields(view)
    assert fields["attempts"] == 2 and fields["first_pass_steps"] == 1 and fields["accepted_steps"] == 1
    assert fields["failure_kinds"] == {"contract": 1}
    assert fields["cost_split"]["first"]["execution"]["provider_priced"]["usd"] == 0.1
    assert "worker" not in fields["cost_split"]["first"]
    assert fields["cost_split"]["retry"]["execution"]["provider_priced"]["usd"] == 0.2
    assert fields["cost_split"]["planning"]["provider_priced"]["usd"] == 0.3
    assert "attempts" not in metrics.attempt_fields({"summary": {"attempts": True}})
    assert metrics.attempt_fields(None) == {}


def test_v2_task_reports_still_compare_and_new_runs_write_v3() -> None:
    old = {"schema": "looper-prompt-eval/v2", "tasks": []}
    assert runner.validate_report(old)["schema"] == "looper-prompt-eval/v2"
    assert runner.validate_report({"schema": runner.SCHEMA, "tasks": []})["schema"] == "looper-prompt-eval/v3"
    assert runner.SCHEMA == "looper-prompt-eval/v3"


def test_claude_version_keeps_the_first_line(tmp_path: Path) -> None:
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\necho '1.2.3 (Claude Code)'\necho extra\n", encoding="utf-8")
    binary.chmod(0o755)
    assert runner.claude_version(str(binary)) == "1.2.3 (Claude Code)"
    assert runner.claude_version(None) is None
    assert runner.claude_version(str(tmp_path / "missing")) is None


def test_two_runs_of_one_set_share_the_table_structure() -> None:
    first = _run(hidden={**_OK, **_ATTEMPTS}, rethink={**_OK, **_ATTEMPTS, "steps": 2})
    second = _run(hidden={**_OK, **_ATTEMPTS, "cost_usd": 0.03}, rethink={**_OK, **_ATTEMPTS, "steps": 3})
    left, right = compare.markdown(first).splitlines(), compare.markdown(second).splitlines()
    assert left[2] == right[2]
    assert all(name in left[2] for name in _NEW_COLUMNS)
    assert len(left[4].split("|")) == len(right[4].split("|")) == left[2].split("|").__len__()
    assert "| dialog | success | cost (USD) |" in compare.markdown(
        {"schema": "looper-manager-dialogs/v2", "mode": "fake", "dialogs": [{"id": "status", **_OK}]}).splitlines()


def test_new_columns_pool_passing_rows_and_old_rows_stay_blank() -> None:
    run = _run(a={**_OK, "attempts": 1, "first_pass_steps": 1, "accepted_steps": 1, "cost_usd": 0.02},
               b={**_OK, "attempts": 3, "first_pass_steps": 0, "accepted_steps": 1, "cost_usd": 0.06})
    table = compare.markdown(run)
    assert _column(table, "**total**", "first-pass rate").startswith("1/2 ")
    assert _column(table, "**total**", "attempts per passing case") == "2.0"
    assert _column(table, "**total**", "cost per passing case (USD)") == "0.0400"
    empty = _run(old={**_OK, "attempts": 1, "first_pass_steps": 0, "accepted_steps": 0})
    assert _column(compare.markdown(empty), "old", "first-pass rate") == "0/0"
    legacy = _run(old=_OK)
    for header in _NEW_COLUMNS:
        assert _column(compare.markdown(legacy), "old", header) == _EN_DASH
        assert _column(compare.markdown(legacy), "**total**", header) == _EN_DASH


def test_markers_are_notes_and_do_not_decide_the_exit_code() -> None:
    passed = [_priced(0.01) | {"id": f"t{i}", "outcome": "pass"} for i in range(10)]
    failed = [_priced(0.01) | {"id": f"t{i}", "outcome": "fail", "success": False} for i in range(10)]
    wide = compare.markdown({"schema": runner.SCHEMA, "mode": "fake", "tasks": failed},
                            {"schema": runner.SCHEMA, "mode": "fake", "tasks": passed})
    assert "- 回退" in wide.splitlines()

    # One case, two reps, all pass, then one of two fails.
    both = {"schema": runner.SCHEMA, "mode": "fake", "git": {"commit": "abcdef1234567"},
            "tasks": [{**_priced(0.02), "id": "case", "rep": 1}, {**_priced(0.02), "id": "case", "rep": 2}]}
    half = {"schema": runner.SCHEMA, "mode": "fake", "git": {"commit": "abcdef1234567"},
            "tasks": [{**_priced(0.02), "id": "case", "rep": 1},
                      {**_priced(0.02), "id": "case", "rep": 2, "outcome": "fail", "success": False}]}
    slipped = compare.markdown(half, both).splitlines()
    assert "- 用例回退: case" in slipped
    assert "- 回退" not in slipped

    base = _run(case=_priced(1.0))
    assert "- 代价回退" in compare.markdown(_run(case=_priced(1.25)), base).splitlines()
    assert "- 代价回退" not in compare.markdown(_run(case=_priced(1.24)), base).splitlines()
    assert "- 代价回退" in compare.markdown(_run(case=_priced(0.01)), _run(case=_priced(0.0))).splitlines()
    assert "- 代价回退" in compare.markdown(_run(case=_priced(1.0, 1.5)), _run(case=_priced(1.0, 1))).splitlines()
    assert "- 代价回退" not in compare.markdown(_run(case=_priced(1.0, 1.4)), _run(case=_priced(1.0, 1))).splitlines()
    dear = _run(case={**_OK, "cost_usd": 1.0})
    cheap = _run(case={**_OK, "cost_usd": 0.01})
    assert "- 代价回退" not in compare.markdown(dear, cheap).splitlines()
    marked = _run(case=_priced(1.25))
    assert cli.exit_code({**marked, "aborted": None}) == 0


def test_committed_baselines_keep_their_tables() -> None:
    fixture = Path(__file__).resolve().parent / "fixtures" / "prompt-eval-baseline-markdown"
    names = ("real-haiku.json", "real-opus.json", "real-sonnet.json",
             "dialogs-real-haiku.json", "dialogs-real-opus.json")
    for name in names:
        data = json.loads((runner.BASELINES_DIR / name).read_text(encoding="utf-8"))
        assert runner.validate_report(data)["schema"]
        found = compare.markdown(data) + "\n"
        assert found == (fixture / f"{name}.md").read_text(encoding="utf-8")


def _failing(task_id: str):
    task = next(item for item in HARD_TASKS if item.id == task_id)
    files = {"hidden_precondition": {"feature.py": _HIDDEN_FEATURE_OK},
             "rethink_bug": {"cart.py": _RETHINK_CART_OK},
             "misleading_error": {"app.py": _MISLEAD_APP_WRONG}}[task_id]
    fake = {**task.fake, "attempts": {"s1": [{"status": "completed", "files": files}]}}
    return dataclasses.replace(task, fake=fake)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("task_id", [task.id for task in HARD_TASKS])
def test_fake_failing_hard_variant_is_a_failure(tmp_path: Path, task_id: str) -> None:
    result, _path = runner.run(mode="fake", tasks=(_failing(task_id),), admin_url=str(DSN), out_dir=tmp_path,
                               agent="claude-code/opus")
    assert result["schema"] == "looper-prompt-eval/v3"
    assert result["runtime_version"] is None
    (row,) = result["tasks"]
    assert row["outcome"] == "fail", row
    assert row["contract_violations"]["total"] == 0
    for key in ("attempts", "first_pass_steps", "accepted_steps", "cost_split", "failure_kinds"):
        assert key in row
