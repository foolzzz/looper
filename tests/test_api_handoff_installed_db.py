"""Opt-in real Claude comparison of the captured production API handoff.

Set LOOPER_TEST_REAL_API_HANDOFF=1 and the disposable database URL. Authentication
uses the macOS Claude keychain login or explicit credential environment variables,
with Claude configuration isolated in a throwaway HOME. Raw inputs/events and the scored report stay
in the ignored evals/runs directory. Normal collection never calls a model.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from evals.prompt_regression import runner
from looper.db import connection
from tests.api_handoff_eval import (SUMMARY, context_switch, prompt_hash, real_available, score, summarize)
from tests.db_support import DSN, fresh_schema


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_captured_handoff_inputs_are_identical_when_the_summary_is_replayed(monkeypatch, tmp_path) -> None:
    async def run():
        first = await context_switch(tmp_path, monkeypatch, SUMMARY)
        replay = await context_switch(tmp_path, monkeypatch, SUMMARY + " Keep the question open.")
        assert first["inputs"] == replay["inputs"]
        assert SUMMARY + " Keep the question open." in replay["resume"]
    asyncio.run(asyncio.wait_for(run(), 45))


@pytest.mark.installed
@pytest.mark.skipif(os.environ.get("LOOPER_TEST_REAL_API_HANDOFF") != "1", reason="real handoff comparison is opt-in")
@pytest.mark.parametrize("model,effort,reps,call_cap,case_cap,total_cap", [
    ("sonnet", "high", 2, 1.0, 1.0, 8.0),
    ("opus", "xhigh", 1, 3.0, 3.0, 12.0),
])
def test_installed_claude_summary_survives_the_production_context_switch(
    monkeypatch, tmp_path, model, effort, reps, call_cap, case_cap, total_cap,
) -> None:
    if not DSN:
        pytest.skip("GAP: disposable PostgreSQL is unavailable")
    if gap := real_available():
        pytest.skip(f"GAP: {gap}")
    output = Path("evals/runs/api-handoff") / model
    output.mkdir(parents=True, exist_ok=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    control_revision = revision
    candidate_diff = subprocess.check_output(["git", "diff", "--binary", "HEAD"])

    def run_one(case: Any, rep: int, cap_left: float | None) -> dict[str, Any]:
        async def run():
            root = tmp_path / f"{case.id}-{rep}"
            captured = await context_switch(root, monkeypatch, SUMMARY, control=case.control)
            async with fresh_schema() as pool:
                await connection.ensure_schema(pool)
                with monkeypatch.context() as scoped:
                    scoped.setattr(connection, "_pool", pool)
                    result = await summarize(root / "claude", captured["inputs"], model=model, effort=effort,
                                             max_budget_usd=min(call_cap, case_cap, cap_left or total_cap))
            try:
                replay = await context_switch(root, monkeypatch, result["summary"], control=case.control)
                assert replay["inputs"] == captured["inputs"], "replay changed the actual handoff inputs"
            except Exception as exc:
                raise runner.TaskError("real summary could not be replayed", cost_usd=result["cost_usd"] or 0) from exc
            row = {**score(result["summary"]), "cost_usd": result["cost_usd"],
                   "prompt_hash": prompt_hash(captured["inputs"]), "replayed": True}
            (output / f"{case.id}-{rep}.json").write_text(json.dumps({**result, **captured, **row}, indent=2))
            return row
        return asyncio.run(asyncio.wait_for(run(), 240))

    cases = tuple(SimpleNamespace(id=name, control=name == "control") for name in ("candidate", "control"))
    rows, stopped = runner.run_suite(cases, run_one, total_cap_usd=total_cap, reps=reps, jobs=1,
                                     run_budget_usd=case_cap)
    # Keep the first Opus outcome even when its one permitted retry succeeds.
    if model == "opus":
        for case in cases:
            first = next(row for row in rows if row["id"] == case.id)
            if first.get("outcome") == "fail":
                spent = sum(float(row.get("cost_usd") or 0) for row in rows)
                if total_cap - spent >= case_cap:
                    rows.append({"id": case.id, "rep": 2, **run_one(case, 2, total_cap - spent)})
    baseline = runner.BASELINES_DIR / "dialogs-real-sonnet.json"
    report = {"model": model, "effort": effort, "candidate_revision": revision,
              "candidate_worktree_dirty": bool(candidate_diff),
              "candidate_worktree_diff_sha256": hashlib.sha256(candidate_diff).hexdigest(),
              "control_revision": control_revision, "rows": rows, "stopped_by_cap": stopped,
              "total_cost_usd": sum(float(row.get("cost_usd") or 0) for row in rows),
              "passed": sum(row.get("outcome") == "pass" for row in rows),
              "failed": sum(row.get("outcome") == "fail" for row in rows),
              "untested": sum(row.get("outcome") not in {"pass", "fail"} for row in rows),
              "invented_action_failures": sum(bool(row.get("invented_action")) for row in rows),
              "sonnet_baseline": "unverified: no matching synthetic case in committed baseline"}
    if baseline.exists():
        report["sonnet_baseline"] += f" ({baseline.name} requires matching case evidence)"
    (output / "report.json").write_text(json.dumps(report, indent=2))
    assert not stopped and all(row.get("outcome") == "pass" for row in rows), report
