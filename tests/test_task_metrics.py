"""Task-quality figures on synthetic rows: no database, one case per figure and boundary."""

from datetime import datetime, timedelta, timezone

import pytest

from looper.agents import metrics as m

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def data(**parts) -> m.Data:
    d = m.Data(NOW - timedelta(days=7), NOW, m.Filters(**parts.pop("filters", {})))
    d.parts.update({"tasks": [], "attempts": [], "contract": {}, "records": []}, **parts)
    return d


def run(task="t", run_no=1, outcome="completed", stop_cause="planner_end", failure_kind=None, last=True, **extra):
    base = NOW - timedelta(hours=5)
    return {"id": 1, "task_id": task, "run_no": run_no, "outcome": outcome, "stop_cause": stop_cause,
            "failure_kind": failure_kind, "started_at": base, "ended_at": base + timedelta(minutes=10),
            "human_wait_ms": 0, "provider_priced_usd": 0.0, "equivalent_usd": 0.0, "unpriced_runs": 0, "last": last,
            "task_created_at": base - timedelta(minutes=2), "supplemented": False, "revised": False,
            "feedback": None, **extra}


_ids = iter(range(1, 10_000))


def attempt(step="s1", n=1, trigger="first", decision="accept", task="t", run_no=1, hours_ago=3, **extra):
    decided = NOW - timedelta(hours=hours_ago) if decision else None
    return {"id": next(_ids), "task_id": task, "task_run_no": run_no, "step_key": step, "step_id": step,
            "attempt": n, "trigger": trigger, "profile_id": "p", "worker_runs": 1, "run_outcome": "completed",
            "review_outcome": None, "review_runs": 0, "blocking_findings": 0, "adjudicated": False,
            "decision": decision, "failure_kind": None, "started_at": NOW - timedelta(hours=hours_ago + 1),
            "decided_at": decided, "difficulty": None, **extra}


def test_wilson_interval_and_the_twenty_case_threshold():
    low, high = m.wilson(10, 20)
    assert (round(low, 4), round(high, 4)) == (0.2993, 0.7007)
    assert m.ratio(19, 19) == {"insufficient": True, "n": 19}
    full = m.ratio(19, 20)
    assert full["n"] == 20 and full["k"] == 19 and full["value"] == 0.95
    assert full["wilson"] == pytest.approx([0.7639, 0.9911], abs=1e-4)
    assert m.ratio(0, 0) == {"insufficient": True, "n": 0}


def test_percentiles_interpolate_and_empty_is_null():
    assert m.spread([]) == {"n": 0, "p50_ms": None, "p95_ms": None}
    assert m.spread([10, 20, 30, 40]) == {"n": 4, "p50_ms": 25, "p95_ms": pytest.approx(38.5)}


def test_task_ratios_exclude_cancelled_restarted_and_earlier_runs():
    rows = ([run(task=f"c{i}") for i in range(15)] + [run(task=f"f{i}", outcome="failed", stop_cause="task_budget",
            failure_kind="task_budget") for i in range(4)]
            + [run(task="cancelled", outcome="cancelled", stop_cause="user_cancel"),
               run(task="restarted", outcome="stopped", stop_cause="restart"),
               run(task="again", run_no=1, outcome="failed", stop_cause="error", failure_kind="error", last=False),
               run(task="again", run_no=2, last=True),
               run(task="done", run_no=2, last=True)])
    d = data(tasks=rows)
    assert len(d.denominator) == 15 + 4 + 2
    assert m.task_success_rate(d)["k"] == 17 and m.task_success_rate(d)["n"] == 21
    assert m.task_budget_failure_rate(d)["k"] == 4
    assert m.task_restarts(d) == {"count": 2}
    assert m.task_success_rate(data(tasks=rows[:15] + rows[15:18])) == {"insufficient": True, "n": 18}


def test_task_cost_keeps_unknown_apart_from_zero_and_both_bases():
    rows = [run(task="a", provider_priced_usd=2.0, equivalent_usd=0.0),
            run(task="b", provider_priced_usd=4.0, equivalent_usd=1.0),
            run(task="c", provider_priced_usd=0.0, equivalent_usd=0.0, unpriced_runs=2),
            run(task="d", outcome="failed", stop_cause="error", provider_priced_usd=99.0)]
    cost = m.task_cost_per_completed(data(tasks=rows))
    assert cost["n"] == 3
    assert cost["provider_priced"] == {"mean_usd": 3.0, "n_known": 2, "unknown": 1}
    assert cost["equivalent"]["mean_usd"] == 0.5
    assert m.task_cost_per_completed(data(tasks=[run(unpriced_runs=1)]))["provider_priced"]["mean_usd"] is None


def test_durations_agent_time_and_queue_time():
    rows = [run(task="a", human_wait_ms=60_000), run(task="b", run_no=2)]
    assert m.task_duration(data(tasks=rows))["p50_ms"] == 600_000
    assert m.task_agent_duration(data(tasks=rows))["n"] == 2
    assert m.task_agent_duration(data(tasks=rows[:1]))["p50_ms"] == 540_000
    assert m.task_queue_time(data(tasks=rows)) == {"n": 1, "p50_ms": 120_000.0, "p95_ms": 120_000.0}


def test_user_corrections_are_three_separate_ratios():
    rows = [run(task=f"t{i}", supplemented=i < 3, revised=i < 5, feedback="defect" if i < 1 else None)
            for i in range(20)]
    c = m.task_user_corrections(data(tasks=rows))
    assert (c["supplement"]["k"], c["revision"]["k"], c["feedback"]["k"]) == (3, 5, 1)


def test_first_pass_counts_original_steps_accepted_at_their_only_attempt_and_leaves_replanned_apart():
    attempts = []
    for i in range(14):
        attempts.append(attempt(f"ok{i}"))
    for i in range(4):  # retried, then accepted
        attempts += [attempt(f"re{i}", 1, decision="retry"), attempt(f"re{i}", 2, "retry")]
    attempts += [attempt("failed", 1, decision="fail", failure_kind="run_error")]
    attempts += [attempt("late", 1), attempt("late", 2, "reopen_late_review")]  # accepted, then reopened
    attempts += [attempt("skipped", 1, decision="skip"), attempt("open", 1, decision=None)]
    attempts += [attempt(f"new{i}", 1, "replanned") for i in range(5)]
    d = data(attempts=attempts)
    first = m.step_first_pass_rate(d)
    assert (first["n"], first["k"]) == (14 + 4 + 1 + 1, 14)
    replans = m.step_replans(d)
    assert replans["steps_added"] == 5 and replans["decisions"] == 0
    assert m.step_replans(data(attempts=[attempt("x", 1, decision="replan")]))["decisions"] == 1


def test_attempts_per_accepted_step_distribution_and_rework_rate():
    attempts = [attempt("a")] + [attempt("b", 1, decision="retry"), attempt("b", 2, "retry")]
    attempts += [attempt("c", 1, decision="retry"), attempt("c", 2, "retry", decision="retry"), attempt("c", 3, "retry")]
    attempts += [attempt("d", i, "retry" if i > 1 else "first", decision="retry" if i < 5 else "accept")
                 for i in range(1, 6)]
    d = data(attempts=attempts)
    spread = m.step_attempts_per_accepted(d)
    assert spread["n"] == 4 and spread["distribution"] == {"1": 1, "2": 1, "3": 1, "4+": 1}
    assert spread["mean"] == (1 + 2 + 3 + 5) / 4
    assert m.step_rework_rate(d) == {"insufficient": True, "n": len(attempts)}
    many = data(attempts=[attempt(f"s{i}", 1, "retry" if i < 5 else "first") for i in range(20)])
    assert (m.step_rework_rate(many)["k"], m.step_rework_rate(many)["n"]) == (5, 20)


def test_failure_kinds_count_the_attempts_that_were_not_accepted():
    d = data(attempts=[attempt("a", decision="retry", failure_kind="timeout"), attempt("b", decision="fail",
                                                                                         failure_kind="timeout"),
                       attempt("c", decision="skip"), attempt("d")])
    assert m.step_failure_kinds(d) == {"n": 3, "counts": {"timeout": 2, "none": 1}}


def test_review_block_effective_misses_and_catch_rate():
    attempts = []
    for i in range(12):  # blocked, fixed and accepted
        attempts += [attempt(f"e{i}", 1, decision="retry", review_outcome="not_passed", review_runs=1),
                     attempt(f"e{i}", 2, "retry", review_outcome="passed", review_runs=1)]
    attempts += [attempt(f"x{i}", 1, decision="fail", review_outcome="not_passed", review_runs=1) for i in range(3)]
    attempts += [attempt(f"m{i}", 1, review_outcome="passed", review_runs=1) for i in range(5)]
    attempts += [attempt(f"m{i}", 2, "reopen_verification", decision="retry") for i in range(5)]
    d = data(attempts=attempts)
    blocks = m.review_effective_blocks(d)
    assert (blocks["count"], blocks["blocked"]) == (12, 15) and blocks["rate"] == {"insufficient": True, "n": 15}
    assert m.review_misses(d) == {"count": 5}
    catch = m.review_catch_rate(d)
    assert catch == {"insufficient": True, "n": 17}
    assert m.review_catch_rate(data(attempts=[attempt(f"c{i}", review_outcome="passed") for i in range(1)])) == {
        "insufficient": True, "n": 0}
    block = m.review_block_rate(d)
    assert block["n"] == 12 * 2 + 3 + 5 and block["k"] == 15


def test_adjudication_rate_counts_blocking_verdicts_and_defects_are_task_counts():
    attempts = [attempt(f"a{i}", review_outcome="passed_by_adjudication", adjudicated=True, blocking_findings=1)
                for i in range(5)]
    attempts += [attempt(f"b{i}", 1, decision="retry", review_outcome="not_passed", blocking_findings=2)
                 for i in range(15)]
    attempts += [attempt("quiet", review_outcome="passed")]
    d = data(attempts=attempts, tasks=[run(task="a", feedback="defect"), run(task="b", feedback="other"), run(task="c")])
    assert (m.review_adjudication_rate(d)["k"], m.review_adjudication_rate(d)["n"]) == (5, 20)
    assert m.review_defective_deliveries(d) == {"count": 1, "completed": 3, "by_verdict": {"defect": 1, "other": 1}}


def test_contract_rates_use_planner_turns_not_attempt_linked_records():
    counts = {"worker_runs": 30, "worker_contract": 3, "reviewer_runs": 10, "reviewer_contract": 1,
              "planner_turns": 40, "planner_rejections": 6, "missing_attempts": 0}
    rates = m.contract_rates(data(contract=counts))
    assert rates["worker"]["value"] == 0.1 and rates["reviewer"] == {"insufficient": True, "n": 10}
    assert rates["planner"] == {"n": 40, "k": 6, "value": 0.15}


def test_run_error_rate_counts_only_attempts_that_ran():
    attempts = [attempt(f"e{i}", decision="retry", failure_kind="run_cap") for i in range(4)]
    attempts += [attempt(f"o{i}") for i in range(16)] + [attempt("n", decision="skip", worker_runs=0)]
    rate = m.run_error_rate(data(attempts=attempts))
    assert (rate["k"], rate["n"]) == (4, 20)


def test_a_profile_filter_keeps_steps_with_an_attempt_of_that_profile():
    attempts = [attempt("a", profile_id="other"), attempt("b", 1, decision="retry"), attempt("b", 2, "retry")]
    d = data(attempts=attempts, filters={"profile": "p"})
    assert [s.key for s in d.steps] == ["b"]
    assert len(d.decided) == 2
    assert [s.key for s in data(attempts=attempts).steps] == ["a", "b"]


def record(id, kind, cost, parent=None, attempt_id=None, equivalent=False):
    return {"id": id, "kind": kind, "cost_usd": cost, "parent_record_id": parent, "attempt_id": attempt_id,
            "equivalent": equivalent}


def test_cost_tree_attributes_by_ancestor_and_never_drops_unattributed_spend():
    records = [
        record(1, "planner", 0.5),                                  # planning before any step
        record(2, "worker", 1.0, attempt_id=10),
        record(3, "reviewer", 0.25, attempt_id=10),
        record(4, "planner", 0.125, attempt_id=10),                 # a deciding turn: planning
        record(5, "planner", 0.0625, attempt_id=10),                # an advise turn under the attempt: planning too
        record(6, "advisor", 0.5, parent=5),                        # its consult: the attempt's advisor spend
        record(7, "advice_check", 0.25, parent=6),                  # the judge below it
        record(8, "planner", 0.03125),                              # a checkpoint turn, never followed by proceed
        record(9, "advisor", 2.0, parent=8),                        # so this stays unattributed
        record(10, "worker", None, attempt_id=None, equivalent=True),   # no attempt, cost unknown
        record(11, "worker", 3.0, parent=999),                      # its parent is gone
        record(12, "worker", 4.0, attempt_id=11, equivalent=True),
    ]
    tree = m.attribute(records)
    assert set(tree.attempts) == {10, 11}
    first = tree.attempts[10]
    assert (first["worker"].usd["provider_priced"], first["review"].usd["provider_priced"],
            first["advisor"].usd["provider_priced"]) == (1.0, 0.25, 0.75)
    assert tree.planning.usd["provider_priced"] == 0.5 + 0.125 + 0.0625 + 0.03125
    assert tree.unattributed["advisor"].usd["provider_priced"] == 2.0
    assert tree.unattributed["worker"].view()["provider_priced"] == {"usd": 3.0, "runs": 1, "unknown_runs": 0}
    assert tree.unattributed["worker"].view()["equivalent"] == {"usd": None, "runs": 1, "unknown_runs": 1}
    total = m._sum([*tree.attempts[10].values(), *tree.attempts[11].values(), tree.planning,
                    *tree.unattributed.values()])
    assert total.usd["provider_priced"] == pytest.approx(sum(r["cost_usd"] for r in records
                                                           if r["cost_usd"] and not r["equivalent"]))


def test_cost_tree_survives_a_parent_cycle():
    tree = m.attribute([record(1, "advisor", 1.0, parent=2), record(2, "advisor", 1.0, parent=1)])
    assert tree.unattributed["advisor"].usd["provider_priced"] == 2.0


def test_step_cost_splits_first_attempt_and_retries_and_keeps_unknown_apart():
    a1, a2, b1 = attempt("a", 1, decision="retry"), attempt("a", 2, "retry"), attempt("b")
    records = [record(1, "worker", 1.0, attempt_id=a1["id"]), record(2, "reviewer", 0.5, attempt_id=a1["id"]),
               record(3, "worker", 2.0, attempt_id=a2["id"]), record(4, "worker", None, attempt_id=b1["id"])]
    cost = m.step_cost_per_accepted(data(attempts=[a1, a2, b1], records=records))
    paid = cost["provider_priced"]
    assert cost["n"] == 2
    assert paid["total"] == {"mean_usd": 3.5, "n_known": 1, "unknown": 1}
    assert paid["first"]["mean_usd"] == 1.5 and paid["retry"]["mean_usd"] == 1.0
    assert cost["equivalent"]["total"] == {"mean_usd": 0.0, "n_known": 2, "unknown": 0}


def test_one_failed_part_costs_only_its_figures():
    d = data(tasks=RuntimeError("tasks unreadable"))
    result = m._evaluate(m.task_success_rate, d)
    assert result == {"error": "RuntimeError: tasks unreadable"}
    assert "error" not in m._evaluate(m.step_rework_rate, d)
