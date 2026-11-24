"""The drafting eval exercises the production input and parser with scripted model replies."""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from unittest.mock import AsyncMock

import pytest

from evals.processor.memory_draft import CASES, evaluate_case


def test_fake_drafting_cases_validate_production_replies_and_boundaries(tmp_path):
    async def run():
        return [await evaluate_case(key, 1, tmp_path / key, mode="fake") for key in CASES]

    results = asyncio.run(run())
    assert len(results) == 12
    assert all(result["passed"] for result in results), results
    assert all(result["mode"] == "fake" and result["cost_usd"] == 0 for result in results)
    assert all(result["checks"]["private_request"] for result in results)
    injected = next(result for result in results if result["case"] == "memory_draft_note_injection")
    assert injected["checks"]["escape_refused"]
    lead = next(result for result in results if result["case"] == "memory_draft_lead_injection")
    assert lead["checks"]["external_never_preference"] and lead["checks"]["flagged"]
    commitment = next(result for result in results if result["case"] == "memory_draft_lead_commitment")
    assert commitment["checks"]["one_entry"] and commitment["checks"]["no_hard_rule"]
    decision = next(result for result in results if result["case"] == "memory_draft_lead_decision")
    assert decision["checks"]["decision_format"] and decision["checks"]["no_instructions"]


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


def test_lead_eval_seeds_use_production_evidence_without_an_operator_quote(tmp_path):
    from evals.processor.memory_draft import _fixture

    for scenario in ("lead_injection", "lead_commitment", "lead_decision"):
        _, _, seed, _, _ = _fixture(tmp_path / scenario, scenario)
        assert seed["source_kind"] == "lead" and seed["source_class"] == "external"
        assert seed["untrusted"] is True and "quote" not in seed
        assert seed["sources"] == [{"lead_id": 7, "text": seed["statement"] + "\n" + seed["why"]}]


@pytest.mark.parametrize(("quote", "leak"), [
    (None, None), ("", None), (" \t\n", None),
    ("Synthetic private quote.", None),
    ("Synthetic private quote.", "record"), ("Synthetic private quote.", "event"),
])
def test_lead_record_ignores_blank_quotes_but_detects_leaks(monkeypatch, tmp_path, quote, leak):
    from evals.processor import __main__ as evaluate
    from evals.processor.memory_draft import _fixture
    from looper.db import connection

    _, _, seed, _, _ = _fixture(tmp_path, "lead_commitment")
    if quote is not None:
        seed["quote"] = quote
    event = {"type": "system", "subtype": "init", "tools": [], "mcp_servers": []}
    if leak == "event":
        event["private_text"] = quote
    pool = AsyncMock()
    pool.fetch.return_value = [{"event": json.dumps(event)}]
    pool.fetchval.return_value = json.dumps({"private_text": quote} if leak == "record" else {"status": " \t\n"})
    monkeypatch.setattr(connection, "get_pool", lambda: pool)

    record = asyncio.run(evaluate._record(7, "", [seed["statement"], seed.get("quote", "")]))
    assert record == {"init_tools": 0, "init_mcp_servers": 0, "tool_uses": 0,
                      "text_kept": leak is not None}


def test_record_ignores_empty_and_blank_batch_messages(monkeypatch):
    from evals.processor import __main__ as evaluate
    from looper.db import connection

    pool = AsyncMock()
    pool.fetch.return_value = []
    pool.fetchval.return_value = json.dumps({"status": " \t"})
    monkeypatch.setattr(connection, "get_pool", lambda: pool)

    record = asyncio.run(evaluate._record(7, "[12:00]《Synthetic》 Sender: \n[12:01]《Synthetic》 Sender:  \t"))
    assert record["text_kept"] is False
