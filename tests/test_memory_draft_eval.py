"""The drafting eval exercises the production input and parser with scripted model replies."""

from __future__ import annotations

import asyncio
from argparse import Namespace
from unittest.mock import AsyncMock

import pytest

from evals.processor.memory_draft import CASES, evaluate_case


def test_fake_drafting_cases_validate_production_replies_and_boundaries(tmp_path):
    async def run():
        return [await evaluate_case(key, 1, tmp_path / key, mode="fake") for key in CASES]

    results = asyncio.run(run())
    assert len(results) == 10
    assert all(result["passed"] for result in results), results
    assert all(result["mode"] == "fake" and result["cost_usd"] == 0 for result in results)
    assert all(result["checks"]["private_request"] for result in results)
    injected = next(result for result in results if result["case"] == "memory_draft_note_injection")
    assert injected["checks"]["escape_refused"]
    lead = next(result for result in results if result["case"] == "memory_draft_lead_injection")
    assert lead["checks"]["external_never_preference"] and lead["checks"]["flagged"]


@pytest.mark.parametrize("source", ["draft", "intents"])
@pytest.mark.parametrize("cost", [0.06, None])
def test_real_eval_preserves_role_selection_and_total_budget(monkeypatch, tmp_path, source, cost):
    from evals.processor import __main__ as evaluate, memory_draft
    from looper.agents import processor
    from looper.db import connection

    for name in ("init_pool", "ensure_schema", "close_pool"):
        monkeypatch.setattr(connection, name, AsyncMock())
    prepare, drafts = AsyncMock(), AsyncMock()
    monkeypatch.setattr(evaluate, "_prepare", prepare)
    monkeypatch.setattr(evaluate, "_prepare_drafts", drafts)
    monkeypatch.setattr(processor, "chosen_profile", AsyncMock(return_value={}))
    monkeypatch.setattr(evaluate, "_intents_file_cases", lambda _: ([], []))
    caps = []

    async def draft_case(key, rep, root, **kwargs):
        caps.append(kwargs["cap_usd"])
        return {"cost_usd": cost}

    async def intents_case(args, *rest):
        caps.append(args.budget_usd)
        return {"cost_usd": cost}

    monkeypatch.setattr(memory_draft, "evaluate_case", draft_case)
    monkeypatch.setattr(evaluate, "_intents_file", intents_case)
    args = Namespace(cases=[next(iter(CASES))] if source == "draft" else [], cases_file=None,
                     intents_file="anonymous.jsonl" if source == "intents" else None,
                     budget_usd=0.1, total_budget_usd=0.1, reps=3, claude="unused", timeout=20)
    runs = asyncio.run(evaluate.evaluate(args, "unused", tmp_path))
    assert len(runs) == (2 if cost is not None else 1)
    assert caps == pytest.approx([0.1, 0.04] if cost is not None else [0.1])
    assert prepare.await_count == int(source == "intents")
    assert drafts.await_count == int(source == "draft")
    connection.close_pool.assert_awaited_once()
