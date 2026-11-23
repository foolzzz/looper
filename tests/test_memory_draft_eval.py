"""The drafting eval exercises the production input and parser with scripted model replies."""

from __future__ import annotations

import asyncio

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
