"""The advisor acceptance's stand-in ``claude`` (``evals/advisor_acceptance_claude.py``) and its switch-off check
(``evals/advisor_acceptance.py``): it tells the advisor's and the judge's runs from the others by their contracts,
its stood-in runs read as the runs they stand for, and the switch-off check can fail."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from evals import advisor_acceptance as acceptance
from evals import advisor_acceptance_claude as stand_in
from looper.agents import advice
from looper.agents.advice import parse_verdict
from looper.agents.tiers.planner_protocol import reply_contract


def test_the_stand_in_knows_the_advisors_and_the_judges_runs_by_their_contracts(tmp_path: Path) -> None:
    assert stand_in.ADVISOR_MARK in advice.ADVISOR_CONTRACT and stand_in.JUDGE_MARK in advice.JUDGE_CONTRACT
    planner = reply_contract(3, advisor=True)  # the Planner's prompt names the advisor, but is neither
    for name, text, expected in (("advisor", advice.ADVISOR_CONTRACT, "advisor"),
                                 ("judge", advice.JUDGE_CONTRACT, "judge"), ("planner", planner, None)):
        prompt = tmp_path / f"{name}.md"
        prompt.write_text("role prompt\n\n" + text, encoding="utf-8")
        assert stand_in.role(["-p", "--system-prompt-file", str(prompt)]) == expected
    assert stand_in.role(["-p"]) is None


def test_a_blocking_judge_sends_the_request_back_and_a_rate_limited_run_says_429(tmp_path: Path) -> None:
    (verdict,) = [e for e in stand_in.stand_in("block", "m") if e["type"] == "result"]
    assert parse_verdict(verdict["result"]).blocking == (("internal_name", "problem"),)
    (limited,) = stand_in.stand_in("rate_limited", "m")
    assert limited["api_error_status"] == 429 and limited["is_error"]
    assert stand_in.stand_in("timeout", "m") == []


def test_a_stood_in_run_writes_stream_json_and_any_other_run_is_the_real_claude(tmp_path: Path) -> None:
    prompt = tmp_path / "judge.md"
    prompt.write_text(advice.JUDGE_CONTRACT, encoding="utf-8")
    modes = tmp_path / "modes.json"
    modes.write_text(json.dumps({"judge": "block"}), encoding="utf-8")
    real = tmp_path / "real"
    real.write_text("#!/bin/sh\necho real \"$@\"\n", encoding="utf-8")
    real.chmod(0o755)
    command = [sys.executable, str(acceptance.STAND_IN), str(real), str(modes), "1.5", "--"]
    out = subprocess.run([*command, "--system-prompt-file", str(prompt), "--model", "m"], input='{"x": 1}\n',
                         capture_output=True, text=True, check=True).stdout
    events = [json.loads(line) for line in out.splitlines()]
    assert [e["type"] for e in events] == ["system", "assistant", "result"]
    other = subprocess.run([*command, "-p"], capture_output=True, text=True, check=True).stdout
    assert other.strip() == "real -p --max-budget-usd 1.5"  # capped, as the eval's own claude is
    capped = subprocess.run([*command, "-p", "--max-budget-usd", "1"], capture_output=True, text=True,
                            check=True).stdout
    assert capped.strip() == "real -p --max-budget-usd 1"  # a run's own cap stays


def test_the_switch_off_check_holds_the_pinned_contracts_and_would_see_the_advisor() -> None:
    import hashlib

    contracts = acceptance.off_contracts()
    digest = hashlib.sha256(json.dumps(contracts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert digest == acceptance.pinned_off_digest()
    assert acceptance.ADVISOR_WORDS.search(reply_contract(3, advisor=True))
    assert not any(acceptance.ADVISOR_WORDS.search(text) for text in contracts.values())
