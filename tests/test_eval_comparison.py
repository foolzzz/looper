"""Pure prompt-regression report comparisons, independent of server-backed evals."""

from __future__ import annotations

from typing import Any

import pytest

from evals.prompt_regression import __main__ as cli
from evals.prompt_regression import compare, runner


# --- Comparison -----------------------------------------------------------------------------------------


def _run(**tasks: dict[str, Any]) -> dict[str, Any]:
    return {"schema": runner.SCHEMA, "mode": "real", "model": "haiku", "git": {"commit": "abcdef1234567", "dirty": False},
            "tasks": [{"id": task_id, **values} for task_id, values in tasks.items()]}


_OK = {"outcome": "pass", "success": True, "steps": 1, "retries": 0, "reviews": 0, "contract_violations": {"total": 0},
       "planner_modified_files": 0, "cost_usd": 0.02}


def test_comparison_marks_what_changed_and_sums_the_run() -> None:
    base = _run(single_step=_OK, chat_correction=_OK)
    tokens = {"input": 1200, "output": 2500, "cache_read": 30000, "cache_creation": 800}
    new = _run(single_step={**_OK, "cost_usd": 0.03, "tokens": tokens},
               chat_correction={**_OK, "outcome": "fail", "success": False, "contract_violations": {"total": 2},
                                "problems": ["color.txt holds 'red', expected 'blue'"]})
    new["git"]["dirty"] = True
    table = compare.markdown(new, base)
    lines = table.splitlines()
    assert lines[0] == "Prompt eval: real/haiku @ abcdef123 (1 rep) → real/haiku @ abcdef123+local changes (1 rep)"
    # A run from before tokens were read has none: only the new side shows them.
    # A run from before gates were counted has none either.
    assert ("| single_step | 1/1 | 1.0 | – | 0.0 | 0.0 | – | – | – | – | – | 0.0 | 0.0 | – → **32k / 2.5k** | 0.0200 → **0.0300** |"
            in lines)
    assert "| chat_correction | 1/1 → **0/1** | 1.0 | – | 0.0 | 0.0 | – | – | – | – | – | 0.0 → **2.0** | 0.0 | – | 0.0200 |" in lines
    assert ("| **total** | 2/2 (100%, 34–100%) → **1/2 (50%, 9–91%)** | 2.0 | – | 0.0 | 0.0 | – | – | – | – | – | 0.0 → **2.0** "
            "| 0.0 | – → **32k / 2.5k** | 0.0400 → **0.0500** |" in lines)
    assert "- pass rate changed: chat_correction 1/1 → 0/1" in lines
    assert "- the base and the new run ran each case once: a pass or fail per case is a single sample" in lines
    assert "- chat_correction: color.txt holds 'red', expected 'blue'" in lines


def test_comparison_without_a_base_and_with_skipped_tasks() -> None:
    new = _run(single_step=_OK, chat_correction={"skipped": "the spending cap was reached", "cost_usd": 0.0})
    new.update(stopped_by_cap=True, caps={"total_cap_usd": 0.8})
    lines = compare.markdown(new).splitlines()
    assert lines[0].endswith("(no earlier run to compare with)")
    assert ("| chat_correction | skipped | skipped | skipped | skipped | skipped | skipped | skipped | skipped "
            "| skipped | skipped | skipped "
            "| skipped | skipped | skipped |" in lines)
    assert "| **total** | 1/1 (100%, 21–100%) | 1.0 | – | 0.0 | 0.0 | – | – | – | – | – | 0.0 | 0.0 | – | 0.0200 |" in lines
    assert "- stopped at the spending cap (0.8 USD)" in lines


def _reps(task_id: str, *rows: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"id": task_id, "rep": rep, **row} for rep, row in enumerate(rows, start=1)]


_PASS = {**_OK, "outcome": "pass"}
_FAIL = {**_OK, "outcome": "fail", "success": False, "steps": 2, "cost_usd": 0.04, "problems": ["sum.txt is missing"]}
_TIMEOUT = {"outcome": "error", "success": False, "error_class": "timeout", "error": "cancelled after 900 s",
            "steps": 7, "cost_usd": 0.5}


def test_comparison_of_repeated_runs_counts_the_scored_reps_and_keeps_errors_apart() -> None:
    # The base has one scored pass and one typed timeout for the shared tasks.
    base = _run(single_step={**_PASS, "rep": 1}, dependent_steps={**_TIMEOUT, "rep": 1},
                chat_correction=_OK)
    new = {**_run(), "tasks": [*_reps("single_step", _PASS, _PASS, _PASS),
                               *_reps("dependent_steps", _PASS, _FAIL, _TIMEOUT),
                               *_reps("interface_first", _PASS, {"skipped": "the spending cap was reached"},
                                      {"outcome": "error", "success": False, "error_class": "harness",
                                       "error": "EvalError: POST /api/v1/tasks/detail answered 500"})]}
    lines = compare.markdown(new, base).splitlines()
    assert lines[0] == "Prompt eval: real/haiku @ abcdef123 (1 rep) → real/haiku @ abcdef123 (3 reps)"
    assert "| single_step | 1/1 → **3/3** | 1.0 | – | 0.0 | 0.0 | – | – | – | – | – | 0.0 | 0.0 | – | 0.0200 |" in lines
    # The mean is over the scored reps: the pass and the fail, not the error.
    assert ("| dependent_steps | 0/0 · 1 err → **1/2 · 1 err** | – → **1.5** | – | – → **0.0** | – → **0.0** "
            "| – | – | – | – | – | – → **0.0** | – → **0.0** | – | – → **0.0300** |" in lines)
    assert "| interface_first | 1/1 · 1 err | 1.0 | – | 0.0 | 0.0 | – | – | – | – | – | 0.0 | 0.0 | – | 0.0200 |" in lines
    # The total adds up the tasks' means (one pass through the set), over the tasks both runs have; the new run's
    # own total follows, as it has a task the base lacks.
    assert ("| **total (2 shared tasks)** | 1/1 (100%, 21–100%) · 1 timeout → **4/5 (80%, 38–96%) · 1 timeout** "
            "| 1.0 → **2.5** | – | 0.0 | 0.0 | – | – | – | – | – | 0.0 | 0.0 | – | 0.0200 → **0.0500** |" in lines)
    assert ("| **total (all 3 tasks of the new run)** | 5/6 (83%, 44–97%) · 1 harness, 1 timeout | 3.5 | – | 0.0 "
            "| 0.0 | – | – | – | – | – | 0.0 | 0.0 | – | 0.0700 |" in lines)
    assert "- pass rate changed: dependent_steps 0/0 → 1/2" in lines
    assert "- the base ran each case once: a pass or fail per case is a single sample" in lines
    assert "- no baseline: interface_first" in lines
    assert "- in the base, not in the new run: chat_correction" in lines
    assert "- dependent_steps (rep 2): sum.txt is missing" in lines
    assert "- dependent_steps (rep 3): error (timeout): cancelled after 900 s" in lines
    assert "- interface_first (rep 3): error (harness): EvalError: POST /api/v1/tasks/detail answered 500" in lines


def test_against_a_base_of_more_tasks_the_total_is_over_the_shared_ones() -> None:
    # A base of seven tasks, six passed; the new run has one of them, twice.
    base = _run(**{f"task_{i}": _OK if i < 6 else {**_OK, "outcome": "fail", "success": False} for i in range(7)})
    base["tasks"][0]["id"] = "single_step"
    new = {**_run(), "tasks": _reps("single_step", _PASS, {**_PASS, "cost_usd": 0.04})}
    lines = compare.markdown(new, base).splitlines()
    assert ("| **total (1 shared task)** | 1/1 (100%, 21–100%) → **2/2 (100%, 34–100%)** | 1.0 | – | 0.0 | 0.0 | – | – "
            "| – | – | – | 0.0 | 0.0 | – | 0.0200 → **0.0300** |" in lines)
    assert not any(line.startswith("| **total (all") for line in lines)  # the new run has no task of its own
    assert "- in the base, not in the new run: task_1, task_2, task_3, task_4, task_5, task_6" in lines


def test_against_a_base_of_other_tasks_there_is_no_shared_total() -> None:
    base = _run(dependent_steps=_OK, chat_correction=_OK)
    new = {**_run(), "tasks": _reps("single_step", _PASS, _PASS)}
    lines = compare.markdown(new, base).splitlines()
    assert not any(line.startswith("| **total (0 shared") for line in lines)
    assert ("| **total (all 1 task of the new run)** | 2/2 (100%, 34–100%) | 1.0 | – | 0.0 | 0.0 | – | – | – | – | – | 0.0 | 0.0 | – "
            "| 0.0200 |" in lines)
    assert "- no baseline: single_step" in lines
    assert "- in the base, not in the new run: dependent_steps, chat_correction" in lines


def test_comparison_says_which_tasks_ran_with_user_gates_other_than_the_base() -> None:
    # Unset is on since user gates G2: a base from before (no per-task record) or one run with them off compares
    # another contract.
    old = _run(single_step=_OK, gate_ambiguous=_OK)
    off = {**_run(), "tasks": [{"id": "single_step", **_PASS, "user_gates": "off"},
                               {"id": "gate_ambiguous", **_PASS, "user_gates": "on"}]}
    on = {**_run(), "tasks": [{"id": "single_step", **_PASS, "user_gates": "on"},
                              {"id": "gate_ambiguous", **_PASS, "user_gates": "on"}]}
    assert ("- the base does not record user gates per task (a run before user gates G2, where an unset setting was "
            "off): single_step, gate_ambiguous") in compare.markdown(on, old).splitlines()
    lines = compare.markdown(on, off).splitlines()
    assert "- user gates differ from the base, so these rows compare different contracts: single_step (off → on)" \
        in lines
    assert not any("user gates" in line for line in compare.markdown(on, on).splitlines())


def test_comparison_of_dialogs_shows_success_and_cost() -> None:
    run = {"schema": "looper-manager-dialogs/v2", "mode": "fake", "manager_agent": "claude-code/opus",
           "git": {"commit": "abcdef1234567", "dirty": False},
           "dialogs": _reps("status", {"outcome": "pass", "success": True, "cost_usd": 0.001},
                            {"outcome": "error", "success": False, "error_class": "timeout",
                             "error": "no reply to 'hi' within 60 s", "cost_usd": 0.002})}
    old = {**run, "dialogs": _reps("status", {"outcome": "pass", "success": True, "cost_usd": 0.003})}
    lines = compare.markdown(run, old).splitlines()
    assert lines[0] == "Manager dialogs: fake/claude-code/opus @ abcdef123 (1 rep) → fake/claude-code/opus @ abcdef123 (2 reps)"
    assert "| dialog | success | cost (USD) |" in lines
    assert "| status | 1/1 → **1/1 · 1 err** | 0.0030 → **0.0010** |" in lines
    assert "- status (rep 2): error (timeout): no reply to 'hi' within 60 s" in lines


@pytest.mark.parametrize(("passes", "scored", "interval"), [
    (8, 10, (0.4902, 0.9433)),
    (41, 48, (0.7283, 0.9275)),
    (0, 10, (0.0, 3.8415 / 13.8415)),  # 0 of n: from 0 to z² / (n + z²)
    (10, 10, (10 / 13.8415, 1.0)),  # n of n: from n / (n + z²) to 1
    (0, 0, None),
])
def test_the_wilson_interval(passes: int, scored: int, interval: tuple[float, float] | None) -> None:
    found = compare.wilson(passes, scored)
    assert found == pytest.approx(interval, abs=1e-4) if interval else found is None


@pytest.mark.parametrize(("row", "outcome", "error_class"), [
    ({"outcome": "pass", "success": False, "timed_out": True}, "pass", None),
    ({"outcome": "error", "success": False, "error_class": "harness"}, "error", "harness"),
    ({"outcome": "fail", "success": True}, "fail", None),
    ({"outcome": "error", "error_class": "timeout"}, "error", "timeout"),
    ({"outcome": "error", "error_class": "spending_cap"}, "error", "spending_cap"),
    ({"skipped": "the spending cap was reached"}, "skipped", None),
])
def test_a_rows_outcome_is_read_from_its_typed_fields(row: dict[str, Any], outcome: str,
                                                       error_class: str | None) -> None:
    assert (compare.outcome(row), compare.error_class(row)) == (outcome, error_class)


@pytest.mark.parametrize(("rows", "aborted", "code"), [
    ([_PASS, _PASS], None, 0),
    ([_PASS, _FAIL], None, 1),
    ([_PASS, _TIMEOUT], None, 1),
    ([_PASS, {"skipped": "the spending cap was reached"}], None, 1),
    ([_PASS], "KeyboardInterrupt", 2),
])
def test_the_exit_code_is_zero_only_when_every_row_ran_and_passed(rows: list[dict[str, Any]], aborted: str | None,
                                                                  code: int) -> None:
    assert cli.exit_code({"tasks": [{"id": "t", **row} for row in rows], "aborted": aborted}) == code


