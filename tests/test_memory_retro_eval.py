"""Retrospective evals reuse the production assembly, role and parser with a fake backend only."""

import asyncio
from argparse import Namespace
from unittest.mock import AsyncMock

from evals.processor.memory_retro import CASES, evaluate_case


def test_fake_retrospective_cases_cover_evidence_targets_and_negative_outcomes(tmp_path):
    async def run():
        return [await evaluate_case(key, rep, tmp_path / f"{key}-{rep}", mode="fake")
                for rep in (1, 2) for key in CASES]
    results = asyncio.run(run())
    assert len(results) == 16
    assert all(result["passed"] for result in results), results
    assert all(result["cost_usd"] == 0 and result["mode"] == "fake" for result in results)


def test_real_retro_eval_reserves_the_whole_case_before_starting(monkeypatch, tmp_path):
    from evals.processor import __main__ as runner, memory_retro
    from looper.agents import processor
    from looper.db import connection

    for name in ("init_pool", "ensure_schema", "close_pool"):
        monkeypatch.setattr(connection, name, AsyncMock())
    prepare = AsyncMock()
    monkeypatch.setattr(runner, "_prepare_drafts", prepare)
    monkeypatch.setattr(runner, "_prepare", AsyncMock(side_effect=AssertionError("wrong role")))
    monkeypatch.setattr(processor, "chosen_profile", AsyncMock(return_value={}))
    case = AsyncMock(return_value={"cost_usd": .06})
    monkeypatch.setattr(memory_retro, "evaluate_case", case)
    args = Namespace(cases=[next(iter(CASES))], cases_file=None, intents_file=None,
        budget_usd=.10, total_budget_usd=.10, reps=3, claude="unused", timeout=90)
    runs = asyncio.run(runner.evaluate(args, "unused", tmp_path))
    assert len(runs) == 1 and case.await_count == 1
    prepare.assert_awaited_once_with(args, role="memory_retro")
