"""The advisor comparison's cost estimate (``evals/advisor_compare.py``): priced at the built-in prices, nothing run."""

from __future__ import annotations

import pytest

from evals import advisor_compare
from evals.advisor_compare import Sizes, estimate, per_problem


def test_the_estimate_prices_each_group_from_its_token_sizes() -> None:
    sizes = Sizes(raw_in=100_000, abstract_in=2_000, advice_out=1_000, worker_cache_read=0, worker_cache_write=0,
                  worker_out=10_000, judge_in=1_000, judge_out=100)
    each = per_problem(sizes)
    # Fable 5.2: a 100k cache write at its one-hour rate, 16 USD/M (the writes' duration is not given, so the
    # dearer one), and 1k out at 40 USD/M; Sonnet 5.5: 10k out at 10 USD/M.
    assert each["raw"] == pytest.approx(1.6 + 0.04 + 0.1)
    assert each["abstract"] == pytest.approx(0.032 + 0.04 + 0.0015 + 0.1, abs=1e-4)
    assert each["effort"] == pytest.approx(0.15)
    result = estimate(20, 3, sizes)
    assert result["groups_usd"]["raw"] == pytest.approx(104.4) and result["total_usd"] == pytest.approx(
        round(sum(result["groups_usd"].values()), 2))
    with pytest.raises(ValueError):
        estimate(0, 3)


def test_the_estimate_command_runs_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    assert advisor_compare.main(["estimate", "--problems", "2", "--reps", "1"]) == 0
    out = capsys.readouterr().out
    assert "| total |" in out and "Nothing ran" in out
